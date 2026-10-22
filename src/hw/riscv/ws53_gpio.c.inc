/* WS53 GPIO v150 basic banks. Included by ws53.c.
 * Source: SDK 163d9150 gpio_porting and hal_gpio_v150 register/operation code.
 * Bank-level digital signals only: no pad mux, analog voltage or debounce timing.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_GPIO "ws53-gpio"
OBJECT_DECLARE_SIMPLE_TYPE(WS53GpioState, WS53_GPIO)

struct WS53GpioState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq, output[32], output_enable[32];
    uint32_t width, valid;
    uint32_t input, latch, oen, enable, mask, type, polarity, dedge, edges;
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

static const MemoryRegionOps ws53_gpio_ops = {
    .read = ws53_gpio_read, .write = ws53_gpio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void ws53_gpio_reset(void *opaque)
{
    WS53GpioState *s = opaque;
    /* Deterministic model reset, not silicon reset values. Preserve external
     * pin levels; reset must not pretend that the attached device disappeared. */
    s->latch = s->enable = s->type = s->polarity = s->dedge = s->edges = 0;
    s->oen = s->mask = s->valid;
    ws53_gpio_update(s);
}

static void ws53_gpio_realize(DeviceState *dev, Error **errp)
{
    WS53GpioState *s = WS53_GPIO(dev);
    if (s->width == 0 || s->width > 32) {
        error_setg(errp, "ws53-gpio: invalid bank width %u", s->width);
        return;
    }
    s->valid = s->width == 32 ? UINT32_MAX : (1u << s->width) - 1;
    qemu_register_reset(ws53_gpio_reset, s);
}

static const Property ws53_gpio_properties[] = {
    DEFINE_PROP_UINT32("width", WS53GpioState, width, 32),
};

static void ws53_gpio_init(Object *obj)
{
    WS53GpioState *s = WS53_GPIO(obj);
    memory_region_init_io(&s->iomem, obj, &ws53_gpio_ops, s, TYPE_WS53_GPIO, 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_gpio_input, "pin", 32);
    qdev_init_gpio_out_named(DEVICE(obj), s->output, "out", 32);
    qdev_init_gpio_out_named(DEVICE(obj), s->output_enable, "output-enable", 32);
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
