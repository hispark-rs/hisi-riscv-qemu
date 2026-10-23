/* WS53 temperature polling profile, SDK 163d9150 / RDL 04ced9c.
 * Assumptions: 16 point average, 256us samples, 5ms report cadence,
 * sticky READY/latest result, START restarts the accumulation. A-board START1
 * clears READY while retaining old CODE (not valid until READY). No stop/IRQ.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_TSENSOR "ws53-tsensor"
OBJECT_DECLARE_SIMPLE_TYPE(WS53TsensorState, WS53_TSENSOR)
struct WS53TsensorState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    QEMUTimer *timer;
    uint32_t control, configured, sum, count;
    uint16_t input_code, initial_code, result;
    bool assume_profile, running, ready, clock_on, stall, corrupt_control;
    int64_t remaining, deadline;
    uint64_t starts, samples, reports;
};
static void ws53_tsensor_pause(WS53TsensorState *s)
{
    if (timer_pending(s->timer)) {
        s->remaining = MAX(s->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(s->timer);
    }
}
static void ws53_tsensor_resume(WS53TsensorState *s)
{
    if (s->running && s->clock_on && !s->stall && !s->corrupt_control && !timer_pending(s->timer)) {
        s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining;
        timer_mod(s->timer, s->deadline);
    }
}
static void ws53_tsensor_tick(void *opaque)
{
    WS53TsensorState *s = opaque;
    s->sum += s->input_code; s->count++; s->samples++;
    if (s->count == 16) {
        s->result = s->sum / 16; s->sum = s->count = 0;
        s->ready = true; s->reports++;
        /* Next report is 5ms later: gap plus fifteen 256us samples. */
        s->remaining = 1160000;
    } else {
        s->remaining = 256000;
    }
    ws53_tsensor_resume(s);
}
static uint64_t ws53_tsensor_read(void *opaque, hwaddr off, unsigned size)
{
    WS53TsensorState *s = opaque;
    if (size == 4 && off == 4) { return s->corrupt_control ? 0xdead : s->control; }
    if (size == 4 && off == 8) { return (s->ready ? 2 : 0) | (s->result << 2); }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-tsensor: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_tsensor_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53TsensorState *s = opaque;
    if (size != 4 || s->corrupt_control) { goto invalid; }
    switch (off) {
    case 4:
        if (v != 3) { goto invalid; } s->control = 3; s->configured |= 1; return;
    case 0x10:
        if (v != 2) { goto invalid; } s->configured |= 2; return;
    case 0x30:
        if (v != 160) { goto invalid; } s->configured |= 4; return;
    case 0x34:
        if (v) { goto invalid; } s->configured |= 8; return;
    case 0:
        if (v != 1 || s->configured != 15) { goto invalid; }
        if (!s->assume_profile) {
            qemu_log_mask(LOG_GUEST_ERROR, "ws53-tsensor: unimplemented START without assume-profile\n"); return;
        }
        timer_del(s->timer); s->sum = s->count = 0; s->ready = false;
        /* Hardware retains the previous CODE field through a new START.
         * Software must still reject it while READY is clear. */
        s->starts++; s->running = true; s->remaining = 256000;
        ws53_tsensor_resume(s); return;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-tsensor: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
}
static const MemoryRegionOps ws53_tsensor_ops = {
    .read=ws53_tsensor_read, .write=ws53_tsensor_write, .endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_tsensor_code(void *opaque, int n, int level)
{
    WS53TsensorState *s = opaque;
    s->input_code = (s->input_code & ~(1u << n)) | (!!level << n);
}
static void ws53_tsensor_clock(void *opaque, int n, int level)
{
    WS53TsensorState *s = opaque;
    ws53_tsensor_pause(s); s->clock_on = level; ws53_tsensor_resume(s);
}
static void ws53_tsensor_stall(void *opaque, int n, int level)
{
    WS53TsensorState *s = opaque;
    ws53_tsensor_pause(s); s->stall = level; ws53_tsensor_resume(s);
}
static void ws53_tsensor_corrupt(void *opaque, int n, int level)
{
    WS53TsensorState *s = opaque;
    ws53_tsensor_pause(s); s->corrupt_control = level; ws53_tsensor_resume(s);
}
static void ws53_tsensor_reset(void *opaque)
{
    WS53TsensorState *s = opaque; timer_del(s->timer);
    s->control = s->configured = s->sum = s->count = s->result = 0;
    s->running = s->ready = false; s->starts = s->samples = s->reports = 0;
    /* External sample code and clock/fault inputs persist across device reset. */
}
static void ws53_tsensor_realize(DeviceState *dev, Error **errp)
{
    WS53TsensorState *s = WS53_TSENSOR(dev);
    if (s->initial_code > 1023) {
        error_setg(errp, "ws53-tsensor: initial-code must fit ten bits"); return;
    }
    s->input_code = s->initial_code;
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-tsensor: assumed profile samples16 period256us report5ms sticky-ready\n");
    }
    qemu_register_reset(ws53_tsensor_reset, s);
}
static const Property ws53_tsensor_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53TsensorState, assume_profile, false),
    DEFINE_PROP_UINT16("initial-code", WS53TsensorState, initial_code, 500),
};
static void ws53_tsensor_init(Object *obj)
{
    WS53TsensorState *s = WS53_TSENSOR(obj); s->clock_on = true;
    memory_region_init_io(&s->mmio, obj, &ws53_tsensor_ops, s, TYPE_WS53_TSENSOR, 0x100);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_tsensor_tick, s);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_tsensor_code, "code", 10);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_tsensor_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_tsensor_stall, "stall", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_tsensor_corrupt, "corrupt-control", 1);
    object_property_add_uint64_ptr(obj, "starts", &s->starts, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "samples", &s->samples, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "reports", &s->reports, OBJ_PROP_FLAG_READ);
}
static void ws53_tsensor_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_tsensor_realize;
    device_class_set_props(dc, ws53_tsensor_properties);
}
static const TypeInfo ws53_tsensor_type = {
    .name=TYPE_WS53_TSENSOR,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53TsensorState),
    .instance_init=ws53_tsensor_init,.class_init=ws53_tsensor_class_init,
};
