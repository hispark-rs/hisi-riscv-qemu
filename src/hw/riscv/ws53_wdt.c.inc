/* WS53 WDT v151 command/count model, SDK 163d9150 / RDL 4939cd2.
 * Timing is NOT established by the SDK: counting requires explicit opt-in to
 * the documented test profile. This is not silicon qualification.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_WDT "ws53-wdt"
OBJECT_DECLARE_SIMPLE_TYPE(WS53WdtState, WS53_WDT)
#define WS53_WDT_KEY 0x5a5a5a5a
#define WS53_WDT_TICK_NS 31250 /* inherited reference application 32000 Hz */
struct WS53WdtState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    QEMUTimer *timer;
    qemu_irq pending_irq, reset_request[1];
    uint32_t load, control, latched, assumed_low_byte;
    int64_t remaining_ns, deadline;
    bool assume_timing, profile_diagnosed, locked, pending, second_stage;
    bool clock_on, stall_snapshot, snapshot_requested, snapshot_valid;
    bool reset_asserted, counting;
};

static void ws53_wdt_irq(WS53WdtState *s)
{
    qemu_set_irq(s->pending_irq, s->pending && !(s->control & 0x40));
}

static bool ws53_wdt_timing(WS53WdtState *s)
{
    if (!s->assume_timing) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-wdt: unimplemented counter timing without assume-timing profile\n");
        return false;
    }
    if (!s->profile_diagnosed) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-wdt: assumed timing low-byte=%u expiry=count+1 stage=reload clock=32000\n",
            s->assumed_low_byte);
        s->profile_diagnosed = true;
    }
    return true;
}

static int64_t ws53_wdt_remaining(WS53WdtState *s)
{
    if (s->counting && s->clock_on) {
        return MAX(s->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 0);
    }
    return s->remaining_ns;
}

static uint32_t ws53_wdt_count(WS53WdtState *s)
{
    int64_t ns = ws53_wdt_remaining(s);
    /* Profile: zero is held for its final input-clock tick before expiry. */
    return ns ? (ns + WS53_WDT_TICK_NS - 1) / WS53_WDT_TICK_NS - 1 : 0;
}

static void ws53_wdt_arm(WS53WdtState *s)
{
    timer_del(s->timer);
    if (s->counting && s->clock_on && !s->reset_asserted) {
        s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining_ns;
        timer_mod(s->timer, s->deadline);
    }
}

static void ws53_wdt_reload(WS53WdtState *s)
{
    uint64_t count = (s->load & 0xffffff00u) | s->assumed_low_byte;
    s->remaining_ns = (count + 1) * WS53_WDT_TICK_NS;
    ws53_wdt_arm(s);
}

static void ws53_wdt_snapshot(WS53WdtState *s)
{
    if (s->snapshot_requested && s->clock_on && !s->stall_snapshot) {
        s->latched = ws53_wdt_count(s);
        s->snapshot_valid = true;
        s->snapshot_requested = false;
    }
}

static void ws53_wdt_expire(void *opaque)
{
    WS53WdtState *s = opaque;
    s->remaining_ns = 0;
    s->pending = true;
    ws53_wdt_irq(s);
    if ((s->control & 4) && (!(s->control & 0x80) || s->second_stage)) {
        s->counting = false;
        s->reset_asserted = true;
        qemu_set_irq(s->reset_request[0], 1);
        /* Topology and pulse timing are not known. A visible request cannot
         * silently masquerade as a completed CPU/SoC reset. */
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-wdt: unimplemented SoC reset topology/pulse (request only)\n");
    } else {
        s->second_stage = true;
        ws53_wdt_reload(s);
    }
}

static uint64_t ws53_wdt_read(void *opaque, hwaddr off, unsigned size)
{
    WS53WdtState *s = opaque;
    if (size == 4) {
        switch (off) {
        case 0: return s->locked;
        case 4: return s->load;
        case 0xc:
            if (s->locked) { break; } /* unknown protection of consuming reads */
            s->pending = false;
            ws53_wdt_irq(s);
            return 0;
        case 0x10: return s->control;
        case 0x14:
            if (!s->snapshot_valid) { break; }
            /* A-board observation: consuming CNT clears CCVR validity.
             * The measured rearm gap is not modeled by this immediate profile. */
            s->snapshot_valid = false;
            return s->latched;
        case 0x18: return s->pending;
        case 0x1c: return s->pending && !(s->control & 0x40);
        case 0x28: return s->snapshot_valid ? 2 : 0;
        }
    }
    /* LPIF/busy states, WO command reads, unsampled CNT and wrong widths are
     * deliberately rejected instead of returning an apparently valid status. */
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-wdt: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}

static void ws53_wdt_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53WdtState *s = opaque;
    uint32_t v = value;
    if (size == 4 && off == 0) {
        s->locked = v != WS53_WDT_KEY;
        return;
    }
    if (size != 4 || (off != 4 && off != 8 && off != 0x10 && off != 0x28)) {
        goto invalid;
    }
    if (s->locked) {
        /* Exact lock coverage is unknown. Reject and diagnose, do not claim
         * that all these operations are ignored by real locked silicon. */
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-wdt: unimplemented locked-write protection +0x%" HWADDR_PRIx "\n", off);
        return;
    }
    switch (off) {
    case 4:
        if (v & 0xff) { goto invalid; }
        s->load = v;
        return;
    case 8:
        if (v != WS53_WDT_KEY) { goto invalid; }
        if (!ws53_wdt_timing(s)) { return; }
        s->second_stage = false;
        ws53_wdt_reload(s); /* feed does not consume raw pending status */
        return;
    case 0x10: {
        if (v & ~0xfdu) { goto invalid; }
        if ((v & 1) && !ws53_wdt_timing(s)) { return; }
        bool was_enabled = s->control & 1;
        s->remaining_ns = ws53_wdt_remaining(s);
        s->control = v;
        s->counting = (v & 1) && !s->reset_asserted;
        if (!was_enabled && (v & 1)) {
            ws53_wdt_reload(s);
        } else {
            ws53_wdt_arm(s);
        }
        ws53_wdt_irq(s);
        return;
    }
    case 0x28:
        if (v != 1) { goto invalid; }
        if (!ws53_wdt_timing(s)) { return; }
        s->snapshot_valid = false;
        s->snapshot_requested = true;
        ws53_wdt_snapshot(s);
        return;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-wdt: unimplemented write +0x%" HWADDR_PRIx " value=0x%x size=%u\n",
        off, v, size);
}

static const MemoryRegionOps ws53_wdt_ops = {
    .read = ws53_wdt_read, .write = ws53_wdt_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_wdt_clock(void *opaque, int n, int level)
{
    WS53WdtState *s = opaque;
    s->remaining_ns = ws53_wdt_remaining(s);
    s->clock_on = level;
    ws53_wdt_arm(s);
    ws53_wdt_snapshot(s);
}

static void ws53_wdt_stall(void *opaque, int n, int level)
{
    WS53WdtState *s = opaque;
    s->stall_snapshot = level;
    ws53_wdt_snapshot(s);
}

static void ws53_wdt_reset(void *opaque)
{
    WS53WdtState *s = opaque;
    timer_del(s->timer);
    /* Deterministic test initialization, not asserted silicon reset values.
     * Preserve externally supplied clock/stall input levels. */
    s->load = s->control = s->latched = 0;
    s->remaining_ns = 0;
    s->locked = true;
    s->pending = s->second_stage = s->counting = s->reset_asserted = false;
    s->snapshot_requested = s->snapshot_valid = false;
    ws53_wdt_irq(s);
    qemu_set_irq(s->reset_request[0], 0);
}

static void ws53_wdt_realize(DeviceState *dev, Error **errp)
{
    WS53WdtState *s = WS53_WDT(dev);
    if (s->assumed_low_byte > 255) {
        error_setg(errp, "ws53-wdt assumed-load-low-byte must be 0..255");
        return;
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_wdt_expire, s);
    qemu_register_reset(ws53_wdt_reset, s);
}

static const Property ws53_wdt_properties[] = {
    DEFINE_PROP_BOOL("assume-timing", WS53WdtState, assume_timing, false),
    DEFINE_PROP_UINT32("assumed-load-low-byte", WS53WdtState, assumed_low_byte, 255),
};

static void ws53_wdt_init(Object *obj)
{
    WS53WdtState *s = WS53_WDT(obj);
    s->clock_on = true; /* explicit inherited SDK clock test profile */
    memory_region_init_io(&s->mmio, obj, &ws53_wdt_ops, s, TYPE_WS53_WDT, 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->pending_irq);
    qdev_init_gpio_out_named(DEVICE(obj), s->reset_request, "reset-request", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_wdt_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_wdt_stall, "stall-snapshot", 1);
}

static void ws53_wdt_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_wdt_realize;
    device_class_set_props(dc, ws53_wdt_properties);
}
static const TypeInfo ws53_wdt_type = {
    .name = TYPE_WS53_WDT, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53WdtState), .instance_init = ws53_wdt_init,
    .class_init = ws53_wdt_class_init,
};
