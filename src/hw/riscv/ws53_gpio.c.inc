/* WS53 GPIO v150 basic banks. Included by ws53.c.
 * Source: SDK 163d9150 gpio_porting and hal_gpio_v150 register/operation code.
 * Bank-level digital signals only: no pad mux, analog voltage or debounce timing.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qapi/visitor.h"

#define TYPE_WS53_GPIO "ws53-gpio"
OBJECT_DECLARE_SIMPLE_TYPE(WS53GpioState, WS53_GPIO)

struct WS53GpioState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq, output[32], output_enable[32];
    uint32_t width, valid;
    uint32_t input, latch, oen, enable, mask, type, polarity, dedge, edges;
    bool assume_source_control;
    bool clock_access, require_clock;
    uint32_t source_control_initial, source_control_observed;
};

static uint32_t ws53_gpio_raw(WS53GpioState *s)
{
    uint32_t active = ~(s->input ^ s->polarity);
    return ((s->edges & s->type) | (active & ~s->type)) &
           s->enable & s->oen & s->valid;
}

static void ws53_gpio_update(WS53GpioState *s)
{
    qemu_set_irq(s->irq, (ws53_gpio_raw(s) & ~s->mask) != 0);
    for (unsigned n = 0; n < 32; n++) {
        uint32_t bit = 1u << n;
        bool driving = (s->valid & ~s->oen & bit) != 0;
        qemu_set_irq(s->output_enable[n], driving);
        qemu_set_irq(s->output[n], driving && (s->latch & bit));
    }
}

static void ws53_gpio_input(void *opaque, int n, int level)
{
    WS53GpioState *s = opaque;
    if (n < 0 || (unsigned)n >= s->width) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-gpio: unimplemented input %d\n", n);
        return;
    }
    uint32_t bit = 1u << n;
    bool old = (s->input & bit) != 0;
    bool high = level != 0;
    if (high) {
        s->input |= bit;
    } else {
        s->input &= ~bit;
    }
    /* Model policy: latch transitions only for enabled input/edge pins. Mask
     * affects delivery, not capture. Polarity/configuration changes alone do
     * not fabricate an edge. Physical synchronizer timing is not established. */
    if (old != high && (s->oen & s->enable & s->type & bit) &&
        ((s->dedge & bit) || high == ((s->polarity & bit) != 0))) {
        s->edges |= bit;
    }
    ws53_gpio_update(s);
}

static uint64_t ws53_gpio_read(void *opaque, hwaddr off, unsigned size)
{
    WS53GpioState *s = opaque;
    switch (off) {
    case 0x00: return ((s->latch & ~s->oen) | (s->input & s->oen)) & s->valid;
    case 0x04: return s->oen;
    case 0x0c: return s->enable;
    case 0x10: return s->mask;
    case 0x14: return s->type;
    case 0x18: return s->polarity;
    case 0x1c: return s->dedge;
    case 0x20: return 0; /* Only the explicit debounce-disabled mode is modeled. */
    case 0x24: return ws53_gpio_raw(s);
    case 0x28: return ws53_gpio_raw(s) & ~s->mask;
    }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-gpio: unimplemented read +0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void ws53_gpio_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53GpioState *s = opaque;
    uint32_t bits = value & s->valid;
    switch (off) {
    case 0x00: s->latch = bits; break;
    case 0x04: s->oen = bits; break;
    case 0x0c: s->enable = bits; break;
    case 0x10: s->mask = bits; break;
    case 0x14: s->type = bits; break;
    case 0x18: s->polarity = bits; break;
    case 0x1c: s->dedge = bits; break;
    case 0x20:
        if (value == 0) {
            return;
        }
        goto unsupported;
    case 0x2c: s->edges &= ~bits; break;
    case 0x30: s->latch |= bits; break;
    case 0x34: s->latch &= ~bits; break;
    default: goto unsupported;
    }
    ws53_gpio_update(s);
    return;
unsupported:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-gpio: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 "\n", off, value);
}

/* SDK calls this register RW. Only its observation is implemented here:
 * neither selector encodings nor silicon reset values are established.
 * Refuse a missing profile and all guest writes, including writing zero.
 * MEMTX_ERROR is a model diagnostic, not a prediction of hardware bus faults. */
static MemTxResult ws53_gpio_read_with_attrs(void *opaque, hwaddr off,
                                            uint64_t *value, unsigned size,
                                            MemTxAttrs attrs)
{
    WS53GpioState *s = opaque;
    if (s->require_clock && !s->clock_access) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-gpio: disabled AGPIO clock; MMIO read rejected\n");
        *value = 0;
        return MEMTX_ERROR;
    }
    if (size != 4 || (off & 3)) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-gpio: unsupported read width/alignment +0x%" HWADDR_PRIx " size=%u\n", off, size);
        *value = 0;
        return MEMTX_ERROR;
    }
    if (off == 0x08) {
        if (!s->assume_source_control) {
            qemu_log_mask(LOG_GUEST_ERROR,
                "ws53-gpio: unimplemented read +0x8 without explicit SW_CTL observation profile\n");
            *value = 0;
            return MEMTX_ERROR;
        }
        *value = s->source_control_observed;
        return MEMTX_OK;
    }
    *value = ws53_gpio_read(opaque, off, size);
    return MEMTX_OK;
}

static MemTxResult ws53_gpio_write_with_attrs(void *opaque, hwaddr off,
                                             uint64_t value, unsigned size,
                                             MemTxAttrs attrs)
{
    WS53GpioState *s = opaque;
    if (s->require_clock && !s->clock_access) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-gpio: disabled AGPIO clock; MMIO write rejected\n");
        return MEMTX_ERROR;
    }
    if (size != 4 || (off & 3)) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-gpio: unsupported write width/alignment +0x%" HWADDR_PRIx " size=%u\n", off, size);
        return MEMTX_ERROR;
    }
    if (off == 0x08) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-gpio: unimplemented write +0x8 value=0x%" PRIx64
            " (SW_CTL selector writes are outside the observation-only model)\n", value);
        return MEMTX_ERROR;
    }
    ws53_gpio_write(opaque, off, value, size);
    return MEMTX_OK;
}

static const MemoryRegionOps ws53_gpio_ops = {
    .read_with_attrs = ws53_gpio_read_with_attrs,
    .write_with_attrs = ws53_gpio_write_with_attrs,
    .endianness = DEVICE_LITTLE_ENDIAN,
    /* Deliver the whole unsupported transaction to the diagnostic callbacks:
     * a 64-bit write must not split into SW_CTL plus GPIO_INT_EN. */
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_gpio_clock_input(void *opaque, int n, int level)
{
    WS53_GPIO(opaque)->clock_access = level != 0;
}

static void ws53_gpio_reset(void *opaque)
{
    WS53GpioState *s = opaque;
    /* Deterministic model reset, not silicon reset values. Preserve external
     * pin levels; reset must not pretend that the attached device disappeared. */
    s->latch = s->enable = s->type = s->polarity = s->dedge = s->edges = 0;
    s->oen = s->mask = s->valid;
    /* Reset restores the configured observation, not a silicon default.
     * A transient host-only source-change injection must not survive reset. */
    s->source_control_observed = s->source_control_initial;
    ws53_gpio_update(s);
}

static void ws53_gpio_realize(DeviceState *dev, Error **errp)
{
    WS53GpioState *s = WS53_GPIO(dev);
    if (s->width == 0 || s->width > 32) {
        error_setg(errp, "ws53-gpio: invalid bank width %u", s->width);
        return;
    }
    if (!s->assume_source_control && s->source_control_initial != 0) {
        error_setg(errp, "ws53-gpio: source-control-value requires assume-source-control=on");
        return;
    }
    if (s->assume_source_control) {
        error_report("ws53-gpio: opaque SW_CTL observation enabled (width=%u); "
                     "selector encoding and silicon reset are not modeled", s->width);
    }
    s->valid = s->width == 32 ? UINT32_MAX : (1u << s->width) - 1;
    qemu_register_reset(ws53_gpio_reset, s);
}

static const Property ws53_gpio_properties[] = {
    DEFINE_PROP_UINT32("width", WS53GpioState, width, 32),
    DEFINE_PROP_BOOL("require-clock", WS53GpioState, require_clock, false),
    DEFINE_PROP_BOOL("assume-source-control", WS53GpioState, assume_source_control, false),
    DEFINE_PROP_UINT32("source-control-value", WS53GpioState, source_control_initial, 0),
};

static void ws53_gpio_source_get(Object *obj, Visitor *v, const char *name,
                                void *opaque, Error **errp)
{
    uint32_t value = WS53_GPIO(obj)->source_control_observed;
    visit_type_uint32(v, name, &value, errp);
}

static void ws53_gpio_source_set(Object *obj, Visitor *v, const char *name,
                                void *opaque, Error **errp)
{
    WS53GpioState *s = WS53_GPIO(obj);
    uint32_t value;
    if (!visit_type_uint32(v, name, &value, errp)) {
        return;
    }
    if (!s->assume_source_control) {
        error_setg(errp, "ws53-gpio: source injection requires assume-source-control=on");
        return;
    }
    /* Explicit model-only stimulus. It does not select a hardware source or
     * touch latch, direction, input level, pending edges or IRQ delivery. */
    s->source_control_observed = value;
}

static void ws53_gpio_init(Object *obj)
{
    WS53GpioState *s = WS53_GPIO(obj);
    memory_region_init_io(&s->iomem, obj, &ws53_gpio_ops, s, TYPE_WS53_GPIO, 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_gpio_input, "pin", 32);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_gpio_clock_input, "clock-access", 1);
    qdev_init_gpio_out_named(DEVICE(obj), s->output, "out", 32);
    qdev_init_gpio_out_named(DEVICE(obj), s->output_enable, "output-enable", 32);
    object_property_add(obj, "source-control-observation", "uint32",
                        ws53_gpio_source_get, ws53_gpio_source_set, NULL, NULL);
    object_property_set_description(obj, "source-control-observation",
        "Model-only opaque SW_CTL change input; requires explicit profile; reset restores source-control-value");
}

static void ws53_gpio_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_gpio_realize;
    device_class_set_props(dc, ws53_gpio_properties);
}

static const TypeInfo ws53_gpio_type = {
    .name = TYPE_WS53_GPIO, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53GpioState), .instance_init = ws53_gpio_init,
    .class_init = ws53_gpio_class_init,
};

/* SDK 163d9150 main.c dev_work_clock_init sets 0x57030024.bit0 with a
 * halfword RMW; PM/clock_recover also use full-word values0/1/3.
 * This visibility gate is a conservative diagnostic model. SPI23 observed
 * GPIO1 inaccessible, but did not capture the failing clock word/mepc.
 * No claim about exact silicon faults, pad holding, synchronizer/IRQ timing,
 * 32K sampling, power or shared-peer ownership is made here.
 * Initial values are integration profiles, NOT silicon reset values:
 * RAM inherits the factory-open gate; FlashBoot qualification starts closed. */
#define TYPE_WS53_AGPIO_CLOCK "ws53-agpio-clock"
OBJECT_DECLARE_SIMPLE_TYPE(WS53AgpioClockState, WS53_AGPIO_CLOCK)
struct WS53AgpioClockState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq gate;
    uint32_t value, initial_value;
    bool write_blocked, change_source;
};
static void ws53_agpio_clock_update(WS53AgpioClockState *s)
{
    qemu_set_irq(s->gate, (s->value & 1) != 0);
}
static MemTxResult ws53_agpio_clock_read(void *opaque, hwaddr off,
                                       uint64_t *value, unsigned size,
                                       MemTxAttrs attrs)
{
    WS53AgpioClockState *s = opaque;
    if (off || (size != 2 && size != 4)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-agpio-clock: unsupported read +0x%" HWADDR_PRIx " size=%u\n", off, size);
        *value = 0;
        return MEMTX_ERROR;
    }
    *value = s->value & (size == 2 ? 0xffffu : UINT32_MAX);
    return MEMTX_OK;
}
static MemTxResult ws53_agpio_clock_write(void *opaque, hwaddr off,
                                        uint64_t value, unsigned size,
                                        MemTxAttrs attrs)
{
    WS53AgpioClockState *s = opaque;
    if (off || (size != 2 && size != 4)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-agpio-clock: unsupported write +0x%" HWADDR_PRIx " size=%u\n", off, size);
        return MEMTX_ERROR;
    }
    if (!s->write_blocked) {
        s->value = size == 2 ? (s->value & 0xffff0000u) | (value & 0xffffu) : value;
        if (s->change_source) { s->value ^= 2; }
        ws53_agpio_clock_update(s);
    }
    return MEMTX_OK;
}
static const MemoryRegionOps ws53_agpio_clock_ops = {
    .read_with_attrs = ws53_agpio_clock_read,
    .write_with_attrs = ws53_agpio_clock_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};
static void ws53_agpio_clock_blocked(void *opaque, int n, int level)
{
    WS53_AGPIO_CLOCK(opaque)->write_blocked = level != 0;
}
static void ws53_agpio_clock_source(void *opaque, int n, int level)
{
    WS53_AGPIO_CLOCK(opaque)->change_source = level != 0;
}
static void ws53_agpio_clock_reset(void *opaque)
{
    WS53AgpioClockState *s = opaque;
    s->value = s->initial_value;
    ws53_agpio_clock_update(s);
}
static void ws53_agpio_clock_realize(DeviceState *dev, Error **errp)
{
    qemu_register_reset(ws53_agpio_clock_reset, WS53_AGPIO_CLOCK(dev));
}
static const Property ws53_agpio_clock_properties[] = {
    DEFINE_PROP_UINT32("initial-value", WS53AgpioClockState, initial_value, 1),
};
static void ws53_agpio_clock_init(Object *obj)
{
    WS53AgpioClockState *s = WS53_AGPIO_CLOCK(obj);
    memory_region_init_io(&s->iomem, obj, &ws53_agpio_clock_ops, s, TYPE_WS53_AGPIO_CLOCK, 4);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->gate);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_agpio_clock_blocked, "write-blocked", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_agpio_clock_source, "source-change", 1);
}
static void ws53_agpio_clock_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_agpio_clock_realize;
    device_class_set_props(dc, ws53_agpio_clock_properties);
}
static const TypeInfo ws53_agpio_clock_type = {
    .name = TYPE_WS53_AGPIO_CLOCK, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53AgpioClockState), .instance_init = ws53_agpio_clock_init,
    .class_init = ws53_agpio_clock_class_init,
};
