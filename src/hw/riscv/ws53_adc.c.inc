/* WS53 ADC v155 analog calibration. Register facts: RDL471ef7c / SDK163d9150;
 * explicit digital assumptions: settling5us, calibration100/128/160us, clear
 * commands act on falling pulse edge. No capture/MMIO stubs or physical proof.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_ADC "ws53-adc"
OBJECT_DECLARE_SIMPLE_TYPE(WS53AdcState, WS53_ADC)
struct WS53AdcState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, reset_mmio;
    QEMUTimer *timer;
    uint32_t analog, offset_cfg, offset_en, data_en, cap_cfg, cap_en;
    uint32_t offset_status, offset_result, cap_status, reset_value;
    uint8_t analog_words, analog_phase, pulse[5], input_offset, initial_offset;
    bool assume_profile, gate[2], clock_on, stall[3], stale, cap_error;
    bool offset_refreshed, cap_refreshed, auto_done, manual_done, cap_done;
    int stage; /* -1 idle, 0 automatic offset, 1 manual offset, 2 capacitor */
    int64_t remaining, deadline, analog_started;
    uint64_t starts, completions, failures, soft_resets;
};
static void ws53_adc_pause(WS53AdcState *s)
{
    if (timer_pending(s->timer)) {
        s->remaining = MAX(s->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(s->timer);
    }
}
static bool ws53_adc_clocked(WS53AdcState *s)
{
    return s->gate[0] && s->gate[1] && s->clock_on && (s->reset_value & 0x2000);
}
static void ws53_adc_resume(WS53AdcState *s)
{
    if (s->stage >= 0 && ws53_adc_clocked(s) && !s->stall[s->stage] && !timer_pending(s->timer)) {
        s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining;
        timer_mod(s->timer, s->deadline);
    }
}
static void ws53_adc_cancel(WS53AdcState *s)
{
    timer_del(s->timer); s->stage = -1;
}
static void ws53_adc_tick(void *opaque)
{
    WS53AdcState *s = opaque;
    if (s->stage == 2) {
        s->cap_status = s->cap_error ? 0x101 : 1;
        s->cap_done = !s->cap_error;
        if (s->cap_error) { s->failures++; } else { s->completions++; }
    } else {
        s->offset_status = 1; s->completions++;
        if (!s->stage) { s->offset_result = s->input_offset; s->auto_done = true; }
        else { s->manual_done = true; }
    }
    s->stage = -1;
}
static bool ws53_adc_start(WS53AdcState *s, unsigned stage)
{
    if (!s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc: unimplemented calibration without assume-profile\n"); return false;
    }
    s->stage = stage; s->starts++;
    const int64_t durations[] = {100000,128000,160000};
    s->remaining = durations[stage]; ws53_adc_resume(s); return true;
}
static unsigned ws53_adc_manual_value(WS53AdcState *s)
{
    unsigned v = s->offset_result;
    return v <= 3 ? 0 : v <= 31 ? v - 3 : v <= 59 ? v + 3 : 63;
}
static void ws53_adc_clear_state(WS53AdcState *s)
{
    ws53_adc_cancel(s);
    s->analog = s->offset_cfg = s->offset_en = s->data_en = s->cap_cfg = s->cap_en = 0;
    s->offset_status = s->cap_status = s->offset_result = 0;
    s->analog_words = s->analog_phase = 0; memset(s->pulse, 0, sizeof(s->pulse));
    s->offset_refreshed = s->cap_refreshed = s->auto_done = s->manual_done = s->cap_done = false;
}
static uint64_t ws53_adc_read(void *opaque, hwaddr off, unsigned size)
{
    WS53AdcState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 0x24: return s->analog;
    case 0x2c: return s->offset_cfg;
    case 0x30: return s->offset_en;
    case 0x34: return s->data_en;
    case 0x44: return s->offset_status;
    case 0x50: return s->offset_result;
    case 0x88: return s->cap_cfg;
    case 0x8c: return s->cap_en;
    case 0xa0: return s->cap_status;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size); return 0;
}
static void ws53_adc_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53AdcState *s = opaque;
    if (size != 4 || (off & 3) || !(s->reset_value & 0x2000)) { goto invalid; }
    /* Command-only pulses: no fabricated readable storage or W1C behavior. */
    if (off == 0x38 || off == 0x3c || off == 0x94 || off == 0x98 || off == 0x9c) {
        unsigned i = off == 0x38 ? 0 : off == 0x3c ? 1 : off == 0x94 ? 2 : off == 0x98 ? 3 : 4;
        if (v > 1 || s->stage >= 0) { goto invalid; }
        if (!v && s->pulse[i]) {
            switch (i) {
            case 0:
                if (!s->offset_en) { s->offset_refreshed = true; }
                else {
                    if (!s->data_en || !s->auto_done || s->offset_cfg != ws53_adc_manual_value(s) << 8) { goto invalid; }
                    if (!ws53_adc_start(s, 1)) { s->offset_en = 0; }
                }
                break;
            case 1: s->offset_status = s->stale ? 1 : 0; break;
            case 2: s->cap_refreshed = true; break;
            case 3: s->cap_status = s->stale ? 1 : s->cap_status & 256; break;
            case 4: s->cap_status &= ~256u; break;
            }
        }
        s->pulse[i] = v; return;
    }
    switch (off) {
    case 0x30:
        if (v > 1) { goto invalid; }
        if (!v) { if (s->stage == 0 || s->stage == 1) { ws53_adc_cancel(s); } s->offset_en = 0; return; }
        if (s->offset_en || s->cap_en || s->stage >= 0 || s->analog_phase != 3 || s->offset_status) { goto invalid; }
        if (s->offset_cfg == 1) {
            if (s->data_en || !s->offset_refreshed) { goto invalid; }
            if (!ws53_adc_start(s, 0)) { return; }
            s->auto_done = s->manual_done = s->cap_done = false;
        } else if (!s->auto_done || s->offset_cfg != ws53_adc_manual_value(s) << 8) { goto invalid; }
        s->offset_en = 1; s->offset_refreshed = false; return;
    case 0x8c:
        if (v > 1) { goto invalid; }
        if (!v) { if (s->stage == 2) { ws53_adc_cancel(s); } s->cap_en = 0; return; }
        if (s->offset_en || s->cap_en || s->stage >= 0 || s->analog_phase != 3 || !s->manual_done ||
            s->cap_cfg != 0x349 || !s->cap_refreshed || s->cap_status) { goto invalid; }
        if (!ws53_adc_start(s, 2)) { return; }
        s->cap_en = 1; s->cap_refreshed = false; s->cap_done = false; return;
    default: break;
    }
    if (s->stage >= 0) { goto invalid; }
    switch (off) {
    case 0xf4: case 0xf8: case 0xfc: {
        unsigned i = (off - 0xf4) / 4;
        const uint32_t profile[] = {0x2aa6010, 0x2085, 12};
        if (v != profile[i]) { goto invalid; } s->analog_words |= 1 << i; return;
    }
    case 0x24:
        if (s->offset_en || s->cap_en) { goto invalid; }
        if (v == 0x260 && s->analog_words == 7) {
            s->analog_phase = 1; s->analog_started = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        } else if (v == 0x103f8 && s->analog_phase == 1 &&
                   qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->analog_started >= 5000) { s->analog_phase = 2; }
        else if (v == 0x1ffff && s->analog_phase == 2) { s->analog_phase = 3; }
        else { goto invalid; }
        s->analog = v; return;
    case 0x2c:
        if (s->offset_en || (v != 1 && (v & ~0x3f00u))) { goto invalid; }
        s->offset_cfg = v; return;
    case 0x34:
        if (v > 1) { goto invalid; } s->data_en = v; return;
    case 0x88:
        if (s->cap_en || v != 0x349) { goto invalid; } s->cap_cfg = v; return;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
}
static uint64_t ws53_adc_reset_read(void *opaque, hwaddr off, unsigned size)
{
    WS53AdcState *s = opaque;
    if (!off && size == 4) { return s->reset_value; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc: unimplemented reset read size=%u\n", size); return 0;
}
static void ws53_adc_reset_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53AdcState *s = opaque;
    if (off || size != 4 || ((v ^ s->reset_value) & ~0x2000u)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc: unimplemented reset write size=%u value=0x%" PRIx64 "\n", size, v); return;
    }
    ws53_adc_pause(s);
    if (!(v & 0x2000)) { ws53_adc_clear_state(s); s->soft_resets++; }
    s->reset_value = v; ws53_adc_resume(s);
}
#define WS53_ADC_OPS(name, reader, writer) \
static const MemoryRegionOps name = { .read=reader,.write=writer,.endianness=DEVICE_LITTLE_ENDIAN, \
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true}, \
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true} }
WS53_ADC_OPS(ws53_adc_ops, ws53_adc_read, ws53_adc_write);
WS53_ADC_OPS(ws53_adc_reset_ops, ws53_adc_reset_read, ws53_adc_reset_write);
#undef WS53_ADC_OPS
static void ws53_adc_gate(void *opaque, int n, int level)
{
    WS53AdcState *s = opaque; ws53_adc_pause(s); s->gate[n] = level; ws53_adc_resume(s);
}
static void ws53_adc_clock(void *opaque, int n, int level)
{
    WS53AdcState *s = opaque; ws53_adc_pause(s); s->clock_on = level; ws53_adc_resume(s);
}
static void ws53_adc_stall(void *opaque, int n, int level)
{
    WS53AdcState *s = opaque; ws53_adc_pause(s); s->stall[n] = level; ws53_adc_resume(s);
}
static void ws53_adc_stale(void *opaque, int n, int level)
{
    WS53_ADC(opaque)->stale = level;
}
static void ws53_adc_cap_error(void *opaque, int n, int level)
{
    WS53_ADC(opaque)->cap_error = level;
}
static void ws53_adc_code(void *opaque, int n, int level)
{
    WS53AdcState *s = opaque; s->input_offset = (s->input_offset & ~(1 << n)) | (!!level << n);
}
static void ws53_adc_reset(void *opaque)
{
    WS53AdcState *s = opaque; ws53_adc_clear_state(s); s->reset_value = 0;
    s->starts = s->completions = s->failures = s->soft_resets = 0;
}
static void ws53_adc_realize(DeviceState *dev, Error **errp)
{
    WS53AdcState *s = WS53_ADC(dev);
    if (s->initial_offset > 63) { error_setg(errp, "ws53-adc: offset-code must fit six bits"); return; }
    s->input_offset = s->initial_offset;
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc: assumed calibration settle5us stages100/128/160us falling-edge-clear\n");
    }
    qemu_register_reset(ws53_adc_reset, s);
}
static const Property ws53_adc_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53AdcState, assume_profile, false),
    DEFINE_PROP_UINT8("offset-code", WS53AdcState, initial_offset, 10),
};
static void ws53_adc_init(Object *obj)
{
    WS53AdcState *s = WS53_ADC(obj); s->clock_on = true; s->stage = -1;
    memory_region_init_io(&s->mmio, obj, &ws53_adc_ops, s, TYPE_WS53_ADC, 0x100);
    memory_region_init_io(&s->reset_mmio, obj, &ws53_adc_reset_ops, s, "ws53-adc-reset", 4);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->reset_mmio);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_adc_tick, s);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_gate, "gate", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_stall, "stall", 3);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_stale, "stale", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_cap_error, "cap-error", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_code, "code", 6);
    object_property_add_uint64_ptr(obj, "starts", &s->starts, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "completions", &s->completions, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "failures", &s->failures, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "soft-resets", &s->soft_resets, OBJ_PROP_FLAG_READ);
}
static void ws53_adc_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_adc_realize;
    device_class_set_props(dc, ws53_adc_properties);
}
static const TypeInfo ws53_adc_type = {
    .name=TYPE_WS53_ADC,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53AdcState),
    .instance_init=ws53_adc_init,.class_init=ws53_adc_class_init,
};
