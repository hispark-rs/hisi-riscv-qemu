/* WS53 application RTC v150, SDK 163d9150 / RDL 0ddce57.
 * Two independent 32-bit channels. Timing/mask conventions require explicit
 * opt-in; no common-bank, physical crystal, ROM or low-power emulation.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_RTC "ws53-rtc"
OBJECT_DECLARE_SIMPLE_TYPE(WS53RtcState, WS53_RTC)
#define WS53_RTC_SECOND 1000000000ULL
struct WS53RtcState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    QEMUTimer *timer, *ack_timer;
    uint64_t counter_hz;
    uint32_t ack_delay_ns;
    qemu_irq irq;
    uint32_t load, control, latched, fraction;
    uint64_t remaining;
    int64_t last_ns;
    bool pending, valid, requested, clock_on, stall_snapshot;
    bool assume_timing, profile_diagnosed, sticky_request, request_readback;
    bool expire_on_request;
};

static void ws53_rtc_irq(WS53RtcState *s)
{
    qemu_set_irq(s->irq, s->pending && !(s->control & 8));
}

static bool ws53_rtc_profile(WS53RtcState *s)
{
    if (!s->assume_timing) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-rtc: unimplemented counter/mask timing without assume-timing profile\n");
        return false;
    }
    if (!s->profile_diagnosed) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-rtc: assumed timing clock=%" PRIu64 " expiry=count+1 mask=high start=reload free=wrap\n", s->counter_hz);
        if (s->ack_delay_ns) {
            qemu_log_mask(LOG_GUEST_ERROR,
                "ws53-rtc: assumed EOI synchronization delay=%uns\n", s->ack_delay_ns);
        }
        s->profile_diagnosed = true;
    }
    return true;
}

static void ws53_rtc_sync(WS53RtcState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t elapsed = now - s->last_ns;
    s->last_ns = now;
    if (!(s->control & 1) || !s->clock_on) {
        return;
    }
    uint64_t partial = (elapsed % WS53_RTC_SECOND) * s->counter_hz + s->fraction;
    uint64_t ticks = (elapsed / WS53_RTC_SECOND) * s->counter_hz + partial / WS53_RTC_SECOND;
    s->fraction = partial % WS53_RTC_SECOND;
    if (ticks < s->remaining) {
        s->remaining -= ticks;
        return;
    }
    ticks -= s->remaining;
    s->pending = true;
    unsigned mode = (s->control >> 1) & 3;
    if (mode == 0 || mode == 2) {
        s->control &= ~1u;
        s->remaining = s->fraction = 0;
    } else {
        uint64_t period = mode == 1 ? (uint64_t)s->load + 1 : (1ULL << 32);
        s->remaining = period - ticks % period;
    }
    ws53_rtc_irq(s);
}

static void ws53_rtc_arm(WS53RtcState *s)
{
    timer_del(s->timer);
    if ((s->control & 1) && s->clock_on) {
        /* remaining<=2^32: multiplication fits u64, including a full wrap. */
        uint64_t numerator = s->remaining * WS53_RTC_SECOND - s->fraction;
        uint64_t ns = (numerator + s->counter_hz - 1) / s->counter_hz;
        timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ns);
    }
}

static void ws53_rtc_snapshot(WS53RtcState *s)
{
    if (s->requested && (s->control & 1) && s->clock_on && !s->stall_snapshot) {
        s->latched = s->remaining - 1;
        s->valid = true;
        s->requested = false;
    }
}

static void ws53_rtc_expire(void *opaque)
{
    WS53RtcState *s = opaque;
    ws53_rtc_sync(s);
    ws53_rtc_arm(s);
}

static uint64_t ws53_rtc_read(void *opaque, hwaddr off, unsigned size)
{
    WS53RtcState *s = opaque;
    if (size == 4) {
        ws53_rtc_sync(s);
        switch (off) {
        case 0: return s->load;
        case 8:
            if (!s->valid) { break; }
            return s->latched;
        case 0x10: return s->control | (s->valid ? 0x40 : 0) |
                         (s->sticky_request && s->request_readback ? 0x20 : 0);
        case 0x18: return s->pending;
        case 0x1c: return s->pending && !(s->control & 8);
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-rtc: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}

static void ws53_rtc_ack(void *opaque)
{
    WS53RtcState *s = opaque;
    s->pending = false;
    ws53_rtc_irq(s);
}
static void ws53_rtc_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53RtcState *s = opaque;
    uint32_t v = value;
    if (size != 4) { goto invalid; }
    if (off == 0 && !(s->control & 1)) {
        s->load = v;
        return;
    }
    if (off == 0x14 && v == 1) {
        ws53_rtc_sync(s);
        if (s->pending && s->ack_delay_ns) {
            if (!timer_pending(s->ack_timer)) {
                timer_mod(s->ack_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->ack_delay_ns);
            }
        } else {
            ws53_rtc_ack(s);
        }
        return;
    }
    /* RSTFSM timing is unknown. CNT_LOCK is RO and may be copied by RMW. */
    if (off != 0x10 || (v & ~0x6fu)) { goto invalid; }
    if ((s->control & 1) && (v & 1) && ((s->control ^ v) & 6)) { goto invalid; }
    if (!ws53_rtc_profile(s)) { return; }
    ws53_rtc_sync(s);
    if (s->expire_on_request && (v & 0x20) && (s->control & 1) && !(s->control & 2)) {
        /* Test-only interleaving: expire after the guest read CONTROL but
         * before its request write takes effect. Keep the old write value. */
        s->expire_on_request = false;
        s->control &= ~1u;
        s->pending = true;
        s->remaining = s->fraction = 0;
    }
    bool was_enabled = s->control & 1;
    s->control = v & 0xf;
    s->request_readback = v & 0x20;
    if (!was_enabled && (v & 1)) {
        s->remaining = (uint64_t)s->load + 1;
        s->fraction = 0;
        s->valid = s->requested = false;
    }
    if (v & 0x20) {
        s->valid = false;
        s->requested = true;
    }
    ws53_rtc_snapshot(s);
    ws53_rtc_arm(s);
    ws53_rtc_irq(s);
    return;
invalid:
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-rtc: unimplemented write +0x%" HWADDR_PRIx " value=0x%x size=%u\n",
        off, v, size);
}

static const MemoryRegionOps ws53_rtc_ops = {
    .read = ws53_rtc_read, .write = ws53_rtc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_rtc_clock(void *opaque, int n, int level)
{
    WS53RtcState *s = opaque;
    ws53_rtc_sync(s);
    s->clock_on = level;
    ws53_rtc_snapshot(s);
    ws53_rtc_arm(s);
}

static void ws53_rtc_stall(void *opaque, int n, int level)
{
    WS53RtcState *s = opaque;
    ws53_rtc_sync(s);
    s->stall_snapshot = level;
    ws53_rtc_snapshot(s);
}

static void ws53_rtc_reset(void *opaque)
{
    WS53RtcState *s = opaque;
    timer_del(s->timer);
    timer_del(s->ack_timer);
    s->load = s->control = s->latched = s->fraction = 0;
    s->remaining = 0;
    s->pending = s->valid = s->requested = s->request_readback = false;
    s->last_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    ws53_rtc_irq(s);
    /* Retain external clock/stall inputs and one-time assumption diagnostic. */
}

static void ws53_rtc_expire_on_request(void *opaque, int n, int level)
{
    WS53RtcState *s = opaque;
    s->expire_on_request = level;
}

static void ws53_rtc_realize(DeviceState *dev, Error **errp)
{
    WS53RtcState *s = WS53_RTC(dev);
    if (s->counter_hz != 32768 && s->counter_hz != 32000) {
        error_setg(errp, "ws53-rtc: counter-hz must select SDK32768 or bounded A-board32000 profile");
        return;
    }
    if (s->ack_delay_ns != 0 && s->ack_delay_ns != 32000) {
        error_setg(errp, "ws53-rtc: ack-delay-ns must be0 or explicit32000ns regression profile");
        return;
    }
    qemu_register_reset(ws53_rtc_reset, WS53_RTC(dev));
}
static const Property ws53_rtc_properties[] = {
    DEFINE_PROP_BOOL("assume-timing", WS53RtcState, assume_timing, false),
    DEFINE_PROP_BOOL("sticky-request", WS53RtcState, sticky_request, false),
    DEFINE_PROP_UINT64("counter-hz", WS53RtcState, counter_hz, 32768),
    DEFINE_PROP_UINT32("ack-delay-ns", WS53RtcState, ack_delay_ns, 0),
};
static void ws53_rtc_init(Object *obj)
{
    WS53RtcState *s = WS53_RTC(obj);
    s->clock_on = true;
    memory_region_init_io(&s->mmio, obj, &ws53_rtc_ops, s, TYPE_WS53_RTC, 0x20);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_rtc_expire, s);
    s->ack_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_rtc_ack, s);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_rtc_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_rtc_stall, "stall-snapshot", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_rtc_expire_on_request, "expire-on-request", 1);
}
static void ws53_rtc_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_rtc_realize;
    device_class_set_props(dc, ws53_rtc_properties);
}
static const TypeInfo ws53_rtc_type = {
    .name = TYPE_WS53_RTC, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53RtcState), .instance_init = ws53_rtc_init,
    .class_init = ws53_rtc_class_init,
};
