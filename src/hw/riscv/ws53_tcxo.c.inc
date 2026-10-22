/* WS53 application TCXO v150, SDK 163d9150 / RDL 4939cd2.
 * Digital counter/latch model. Clock handoff, sleep compensation and the
 * control-core window are outside this device. No silicon reset-value claim.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_TCXO "ws53-tcxo"
OBJECT_DECLARE_SIMPLE_TYPE(WS53TcxoState, WS53_TCXO)
struct WS53TcxoState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint64_t counter, latched, initial_count;
    uint32_t clock_hz, fraction, commands;
    int64_t last_ns;
    bool enabled, valid, requested, clock_on, stall_snapshot, sticky_commands;
};

static void ws53_tcxo_sync(WS53TcxoState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t elapsed = now - s->last_ns;
    if (s->enabled && s->clock_on) {
        /* Split before multiplication. The fractional product fits u64 even
         * at UINT32_MAX Hz; whole ticks intentionally wrap modulo 2^64. */
        uint64_t part = (elapsed % 1000000000ULL) * s->clock_hz + s->fraction;
        s->counter += (elapsed / 1000000000ULL) * s->clock_hz + part / 1000000000ULL;
        s->fraction = part % 1000000000ULL;
    }
    s->last_ns = now;
}

static void ws53_tcxo_complete(WS53TcxoState *s)
{
    ws53_tcxo_sync(s);
    if (s->requested && s->enabled && s->clock_on && !s->stall_snapshot) {
        s->latched = s->counter;
        s->valid = true;
        s->requested = false;
    }
}

static uint64_t ws53_tcxo_read(void *opaque, hwaddr off, unsigned size)
{
    WS53TcxoState *s = opaque;
    if (size == 4 && off == 0) {
        return (s->enabled ? 4 : 0) | (s->valid ? 0x10 : 0) |
               (s->sticky_commands ? s->commands : 0);
    }
    if (size == 4 && off >= 4 && off <= 0x10 && !(off & 3) && s->valid) {
        return (s->latched >> ((off / 4 - 1) * 16)) & 0xffff;
    }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-tcxo: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}

static void ws53_tcxo_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53TcxoState *s = opaque;
    uint32_t v = value;
    /* VALID is read-only: allow its copied-back value during SDK-style RMW,
     * but it can never create a ready snapshot. Unknown bits are diagnosed. */
    if (size != 4 || off != 0 || (v & ~0x17u) || (v & 3) == 3) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-tcxo: unimplemented write +0x%" HWADDR_PRIx " value=0x%x size=%u\n",
            off, v, size);
        return;
    }
    ws53_tcxo_sync(s);
    s->enabled = v & 4;
    s->commands = v & 3;
    if (v & 2) {
        s->counter = s->fraction = 0;
        s->valid = s->requested = false;
    }
    if (v & 1) {
        s->valid = false;
        s->requested = true;
    }
    ws53_tcxo_complete(s);
}

static const MemoryRegionOps ws53_tcxo_ops = {
    .read = ws53_tcxo_read, .write = ws53_tcxo_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_tcxo_clock(void *opaque, int n, int level)
{
    WS53TcxoState *s = opaque;
    ws53_tcxo_sync(s);
    s->clock_on = level;
    ws53_tcxo_complete(s);
}

static void ws53_tcxo_stall(void *opaque, int n, int level)
{
    WS53TcxoState *s = opaque;
    s->stall_snapshot = level;
    ws53_tcxo_complete(s);
}

static void ws53_tcxo_reset(void *opaque)
{
    WS53TcxoState *s = opaque;
    s->counter = s->initial_count;
    s->latched = s->fraction = s->commands = 0;
    s->enabled = s->valid = s->requested = false;
    s->last_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* Preserve clock/stall fixture inputs; the shared hardware epoch is not
     * inferred from this deterministic application-window reset profile. */
}

static void ws53_tcxo_realize(DeviceState *dev, Error **errp)
{
    WS53TcxoState *s = WS53_TCXO(dev);
    if (!s->clock_hz) {
        error_setg(errp, "ws53-tcxo clock-frequency must be nonzero");
        return;
    }
    qemu_register_reset(ws53_tcxo_reset, s);
}

static const Property ws53_tcxo_properties[] = {
    DEFINE_PROP_UINT32("clock-frequency", WS53TcxoState, clock_hz, 32000000),
    DEFINE_PROP_UINT64("initial-count", WS53TcxoState, initial_count, 0),
    /* Both possible command readback profiles are tested. No physical
     * auto-clear timing is inferred from SDK helpers that only write ones. */
    DEFINE_PROP_BOOL("sticky-commands", WS53TcxoState, sticky_commands, false),
};

static void ws53_tcxo_init(Object *obj)
{
    WS53TcxoState *s = WS53_TCXO(obj);
    s->clock_on = true; /* inherited SDK clock profile, not clock-register emulation */
    memory_region_init_io(&s->mmio, obj, &ws53_tcxo_ops, s, TYPE_WS53_TCXO, 0x14);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_tcxo_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_tcxo_stall, "stall-snapshot", 1);
}
static void ws53_tcxo_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_tcxo_realize;
    device_class_set_props(dc, ws53_tcxo_properties);
}
static const TypeInfo ws53_tcxo_type = {
    .name = TYPE_WS53_TCXO, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53TcxoState), .instance_init = ws53_tcxo_init,
    .class_init = ws53_tcxo_class_init,
};
