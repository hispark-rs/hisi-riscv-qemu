/* WS53 SDIO initialization/events and opt-in abstract CMD53/ADMA profile.
 * Source: hisi-registers 10ef24b, original ROM init/handler and SDK clock/reset
 * patch. Explicit opt-in assumes latched child status and captured-word ACK.
 * No silicon reset values, CIS enumeration, bus timing or DMA drain claimed.
 * Unsupported transfers and registers diagnose instead of storing/returning 0
 * as if implemented. SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_SDIO "ws53-sdio"
OBJECT_DECLARE_SIMPLE_TYPE(WS53SdioState, WS53_SDIO)
struct WS53SdioState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, reset_mmio;
    qemu_irq irq;
    uint32_t regs[0x300 / 4], seed, reset_control, global_extra;
    bool assume_init, assume_adma, gate[2], host_clock, refuse_ready;
    uint64_t rejections, acknowledgements, host_enables, resets;
    QEMUTimer *dma_timer, *posted_timer;
    GByteArray *host_data, *device_data;
    uint32_t next_command, next_block_size, table_pointer, descriptor_index;
    uint32_t request_length, transferred, descriptor_address, descriptor_left;
    uint32_t posted_address, posted_length;
    uint8_t posted_data[4];
    uint64_t epoch, posted_epoch, transfers, dma_bytes, dma_errors, posted_after_reset;
    uint64_t read_notifications;
    uint32_t offered_length;
    bool offer_pending;
    bool request_pending, dma_active, host_write, posted;
    bool dma_stall, read_error, write_error, delay_posted, suppress_completion, posted_stall;
};
static bool ws53_sdio_accessible(WS53SdioState *s)
{
    return s->assume_init && s->gate[0] && s->gate[1] && (s->reset_control & 2);
}
static uint32_t ws53_sdio_global(WS53SdioState *s)
{
    return s->global_extra |
        ((s->regs[0x38 / 4] & s->regs[0x34 / 4]) ? 1 : 0) |
        ((s->regs[0x110 / 4] & s->regs[0x114 / 4]) ? 4 : 0);
}
static void ws53_sdio_update(WS53SdioState *s)
{
    qemu_set_irq(s->irq, ws53_sdio_accessible(s) &&
                 (ws53_sdio_global(s) & s->regs[0x18 / 4] & 7));
}
static bool ws53_sdio_configured(WS53SdioState *s)
{
    return ws53_sdio_accessible(s) && s->host_clock &&
        s->regs[4 / 4] == 0xb36343 && (s->regs[0x10c / 4] & 0xffffff) == 0xff8000 &&
        (s->regs[0x34 / 4] & 0xfff) == 0xfff &&
        (s->regs[0x114 / 4] & 0x3fff) == 0x3fff &&
        (s->regs[0x118 / 4] & 0x3fff) == 7 &&
        (s->regs[0x24 / 4] & 0x7f) == 0x7f &&
        (s->regs[0x28 / 4] & 0x10) && s->regs[0x2c / 4] == 0x2f92 &&
        (s->regs[0x30 / 4] & 1);
}
static void ws53_sdio_reject(WS53SdioState *s, bool write, hwaddr off, unsigned size)
{
    s->rejections++;
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-sdio: unimplemented/unavailable %s +0x%"
                  HWADDR_PRIx " size=%u\n", write ? "write" : "read", off, size);
}
#include "ws53_sdio_adma.c.inc"
static bool ws53_sdio_rw_mask(hwaddr off, uint32_t *mask)
{
    switch (off) {
    case 0x18: *mask = 7; break;
    case 0x24: *mask = 0x7f; break;
    case 0x28: *mask = 0x10; break; /* Voltage switch deliberately unsupported. */
    case 0x2c: *mask = UINT32_MAX; break;
    case 0x30: *mask = 1; break;
    case 0x34: *mask = 0xfff; break;
    case 0x10c: *mask = 0xffffff; break;
    case 0x114: *mask = 0x3fff; break;
    case 0x118: *mask = 0x3fff; break;
    case 0x11c: *mask = 1; break;
    default: return false;
    }
    return true;
}
static uint64_t ws53_sdio_read(void *opaque, hwaddr off, unsigned size)
{
    WS53SdioState *s = opaque;
    uint32_t mask;
    if (!ws53_sdio_accessible(s) || size != 4 || (off & 3)) {
        goto reject;
    }
    if (ws53_sdio_rw_mask(off, &mask)) {
        return s->regs[off / 4];
    }
    switch (off) {
    case 0x1c: return ws53_sdio_global(s);
    case 0x38: case 0x110: case 0x128: return s->regs[off / 4];
    case 0x44: return s->host_clock;
    default: break;
    }
    if (s->assume_adma && (off == 0x104 || off == 0x120 || off == 0x254 || off == 0x258)) {
        return s->regs[off / 4];
    }
reject:
    ws53_sdio_reject(s, false, off, size);
    return 0;
}
static void ws53_sdio_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53SdioState *s = opaque;
    uint32_t mask, v = value;
    if (!ws53_sdio_accessible(s) || size != 4 || (off & 3)) {
        goto reject;
    }
    if (s->assume_adma && off == 0x40) {
        ws53_sdio_adma_submit(s, v);
        return;
    } else if (s->assume_adma && off == 0x120 &&
               !((v ^ s->regs[off / 4]) & ~0x1fffffu) && s->host_clock && !s->dma_active && !s->posted) {
        s->regs[off / 4] = v;
    } else if (s->assume_adma && off == 0x104 && v == (s->regs[off / 4] | 1) &&
               ws53_sdio_configured(s) && (s->regs[0x11c / 4] & 1) &&
               !s->dma_active && !s->posted && !s->request_pending && !s->offer_pending &&
               (s->regs[0x120 / 4] & 0x1fffff)) {
        s->regs[off / 4] = v;
        s->offered_length = s->regs[0x120 / 4] & 0x1fffff;
        s->offer_pending = true;
        s->read_notifications++;
    } else if (off == 4 && s->host_clock && !(v & 0xff000000)) {
        s->regs[1] = v;
    } else if (off == 0x1c) {
        s->global_extra &= ~v;
        s->acknowledgements++;
    } else if (off == 0x38 || off == 0x110) {
        s->regs[off / 4] &= ~v;
        s->acknowledgements++;
    } else if (ws53_sdio_rw_mask(off, &mask) && !((v ^ s->regs[off / 4]) & ~mask)) {
        if (off != 0x18 && !s->host_clock) {
            goto reject;
        }
        if (off == 0x11c && (v & 1)) {
            if (!s->host_enables || !ws53_sdio_configured(s)) {
                goto reject;
            }
            if (s->refuse_ready) {
                return; /* Explicit failed-readback input, not a successful write. */
            }
        }
        s->regs[off / 4] = v;
    } else {
        goto reject;
    }
    ws53_sdio_update(s);
    return;
reject:
    ws53_sdio_reject(s, true, off, size);
}
static const MemoryRegionOps ws53_sdio_ops = {
    .read = ws53_sdio_read, .write = ws53_sdio_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
    .impl = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
};
static void ws53_sdio_initial_state(WS53SdioState *s)
{
    static const unsigned config[] = {0x18, 0x24, 0x28, 0x2c, 0x30, 0x34,
                                      0x10c, 0x114, 0x118, 0x11c};
    memset(s->regs, 0, sizeof(s->regs));
    for (unsigned i = 0; i < ARRAY_SIZE(config); i++) {
        s->regs[config[i] / 4] = s->seed;
    }
    s->regs[0x18 / 4] &= ~7u;
    s->regs[0x34 / 4] &= ~0xfffu;
    s->regs[0x114 / 4] &= ~0x3fffu;
    s->regs[0x30 / 4] &= ~1u;
    s->regs[0x11c / 4] &= ~1u;
    s->regs[0x104 / 4] = s->seed & ~1u;
    s->regs[0x120 / 4] = s->seed & 0xffe00000;
    s->global_extra = 0;
    s->host_enables = 0;
    ws53_sdio_adma_reset(s);
    ws53_sdio_update(s);
}
static uint64_t ws53_sdio_reset_read(void *opaque, hwaddr off, unsigned size)
{
    WS53SdioState *s = opaque;
    if (s->assume_init && !off && size == 4) {
        return s->reset_control;
    }
    ws53_sdio_reject(s, false, 0x1000 + off, size);
    return 0;
}
static void ws53_sdio_reset_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53SdioState *s = opaque;
    if (!s->assume_init || off || size != 4 || ((v ^ s->reset_control) & ~2u)) {
        ws53_sdio_reject(s, true, 0x1000 + off, size);
        return;
    }
    bool assert_reset = (s->reset_control & 2) && !(v & 2);
    s->reset_control = v;
    if (assert_reset) {
        s->resets++;
        ws53_sdio_initial_state(s);
    }
    ws53_sdio_update(s);
}
static const MemoryRegionOps ws53_sdio_reset_ops = {
    .read = ws53_sdio_reset_read, .write = ws53_sdio_reset_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
    .impl = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
};
static void ws53_sdio_gate(void *opaque, int n, int level)
{
    WS53SdioState *s = opaque;
    s->gate[n] = level;
    ws53_sdio_adma_schedule(s);
    ws53_sdio_update(s);
}
static void ws53_sdio_host(void *opaque, int n, int level)
{
    WS53SdioState *s = opaque;
    if (n == 0) {
        s->host_clock = level;
    } else if (n == 2 && level) {
        ws53_sdio_adma_request(s);
    } else if (n == 1 && level) {
        if (!ws53_sdio_configured(s)) {
            ws53_sdio_reject(s, true, 0x2000, 4);
            return;
        }
        s->host_enables++;
        s->regs[0x110 / 4] |= 1 << 13;
    }
    ws53_sdio_adma_schedule(s);
    ws53_sdio_update(s);
}
static void ws53_sdio_fault(void *opaque, int n, int level)
{
    WS53SdioState *s = opaque;
    switch (n) {
    case 0: s->refuse_ready = level; break;
    case 1: s->dma_stall = level; break;
    case 2: s->read_error = level; break;
    case 3: s->write_error = level; break;
    case 4: s->delay_posted = level; break;
    case 5: s->suppress_completion = level; break;
    case 6: s->posted_stall = level; break;
    }
    ws53_sdio_adma_schedule(s);
}
/* Explicit injected observations for error/IRQ tests; not an SDIO host command
 * parser. Message data is supplied separately as a QOM test input. */
static void ws53_sdio_inject(void *opaque, int n, int level)
{
    WS53SdioState *s = opaque;
    if (!level) { return; }
    if (!ws53_sdio_accessible(s)) {
        ws53_sdio_reject(s, true, 0x3000 + n * 4, 4);
        return;
    }
    if (n < 32) { s->regs[0x110 / 4] |= 1u << n; }
    else if (n < 64) { s->regs[0x38 / 4] |= 1u << (n - 32); }
    else { s->global_extra |= 1u << (n - 64); }
    ws53_sdio_update(s);
}
static void ws53_sdio_reset(void *opaque)
{
    WS53SdioState *s = opaque;
    s->rejections = s->acknowledgements = s->resets = 0;
    s->transfers = s->dma_bytes = s->dma_errors = s->posted_after_reset = 0;
    s->read_notifications = 0;
    s->reset_control = s->seed | 2;
    ws53_sdio_initial_state(s);
}
static void ws53_sdio_realize(DeviceState *dev, Error **errp)
{
    WS53SdioState *s = WS53_SDIO(dev);
    if (s->assume_adma && !s->assume_init) {
        error_setg(errp, "ws53-sdio: ADMA profile requires explicit initialization profile");
        return;
    }
    if (s->assume_adma) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-sdio: assumed CMD53-length-delimited ADMA/latched-status/ACK profile; no CIS, electrical or drain qualification\n");
    } else if (s->assume_init) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-sdio: assumed initialization/latched-status/ACK profile; no CIS, ADMA or host protocol\n");
    }
    qemu_register_reset(ws53_sdio_reset, s);
}
static void ws53_sdio_unrealize(DeviceState *dev)
{
    qemu_unregister_reset(ws53_sdio_reset, WS53_SDIO(dev));
}
static void ws53_sdio_finalize(Object *obj)
{
    WS53SdioState *s = WS53_SDIO(obj);
    timer_free(s->dma_timer);
    timer_free(s->posted_timer);
    g_byte_array_unref(s->host_data);
    g_byte_array_unref(s->device_data);
}
static const Property ws53_sdio_properties[] = {
    DEFINE_PROP_BOOL("assume-init-profile", WS53SdioState, assume_init, false),
    DEFINE_PROP_BOOL("assume-adma-profile", WS53SdioState, assume_adma, false),
    DEFINE_PROP_UINT32("config-seed", WS53SdioState, seed, 0),
};
static void ws53_sdio_init(Object *obj)
{
    WS53SdioState *s = WS53_SDIO(obj);
    memory_region_init_io(&s->mmio, obj, &ws53_sdio_ops, s, TYPE_WS53_SDIO, 0x1000);
    memory_region_init_io(&s->reset_mmio, obj, &ws53_sdio_reset_ops, s, "ws53-sdio-reset", 4);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->reset_mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    ws53_sdio_adma_init(obj);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sdio_gate, "gate", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sdio_host, "host", 3);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sdio_fault, "fault", 7);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sdio_inject, "inject", 96);
    object_property_add_uint32_ptr(obj, "host-message", &s->regs[0x128 / 4], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint64_ptr(obj, "rejections", &s->rejections, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "acknowledgements", &s->acknowledgements, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "host-enables", &s->host_enables, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "resets", &s->resets, OBJ_PROP_FLAG_READ);
}
static void ws53_sdio_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_sdio_realize;
    dc->unrealize = ws53_sdio_unrealize;
    device_class_set_props(dc, ws53_sdio_properties);
}
static const TypeInfo ws53_sdio_type = {
    .name = TYPE_WS53_SDIO, .parent = TYPE_SYS_BUS_DEVICE, .instance_size = sizeof(WS53SdioState),
    .instance_init = ws53_sdio_init, .class_init = ws53_sdio_class_init,
    .instance_finalize = ws53_sdio_finalize,
};
