/* ADC-to-MCU_DIAG capture, explicit assumed digital profile. CPU trace is only
 * inherited ownership state, not an emulated trace generator. One conversion
 * and one posted RAM beat are separate; stopping ADC cannot retract that beat.
 * SPDX-License-Identifier: GPL-2.0-or-later */
static void ws53_adc_capture_pause(WS53AdcState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (timer_pending(s->sample_timer)) {
        s->sample_remaining = MAX(s->sample_deadline - now, 1); timer_del(s->sample_timer);
    }
    if (timer_pending(s->write_timer)) {
        s->write_remaining = MAX(s->write_deadline - now, 1); timer_del(s->write_timer);
    }
}
static void ws53_adc_capture_resume(WS53AdcState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->capture_in_flight && s->diag_clock && !s->bus_stall && !timer_pending(s->write_timer)) {
        s->write_deadline = now + s->write_remaining; timer_mod(s->write_timer, s->write_deadline);
    }
    if (s->scan_running && (s->diag_mode & 0x100) && !s->capture_in_flight && !s->capture_finished &&
        !s->capture_fault && ws53_adc_clocked(s) && s->diag_clock && !s->sample_stall && !timer_pending(s->sample_timer)) {
        s->sample_deadline = now + s->sample_remaining; timer_mod(s->sample_timer, s->sample_deadline);
    }
}
static void ws53_adc_capture_stop(WS53AdcState *s)
{
    timer_del(s->sample_timer); s->scan_running = false;
    /* The independent diagnostic RAM bus master retains any posted beat. */
}
static bool ws53_adc_capture_range(WS53AdcState *s)
{
    uint64_t limit = (uint64_t)s->diag_start + (uint64_t)s->diag_length * 4;
    return s->diag_length && !(s->diag_start & 3) && s->diag_start >= 0x20010000 &&
           limit <= 0x20060000 && limit - 1 == s->diag_end;
}
static void ws53_adc_capture_sample(void *opaque)
{
    WS53AdcState *s = opaque;
    unsigned ch = s->next_channel;
    while (!(s->scan_control & (1u << ch))) { ch = (ch + 1) % 8; }
    s->next_channel = (ch + 1) % 8;
    s->posted_word = ((uint32_t)s->sample_input[ch] << 2) | (ch << 14);
    s->posted_address = s->diag_start + s->diag_count * 4;
    s->posted_last = s->diag_count + 1 == s->diag_length;
    s->capture_in_flight = 1;
    s->write_remaining = s->write_latency_ns;
    if (s->early_done && s->posted_last) {
        /* Adversarial posted-write ordering: visible completion/address before
         * RAM commit. Not a claim about actual silicon ordering. */
        s->diag_done = 1; s->diag_done_address = s->posted_address;
    }
    ws53_adc_capture_resume(s);
}
static void ws53_adc_capture_writeback(void *opaque)
{
    WS53AdcState *s = opaque;
    uint8_t bytes[4]; stl_le_p(bytes, s->posted_word);
    MemTxResult result = s->bus_error ? MEMTX_ERROR :
        address_space_write(&address_space_memory, s->posted_address, MEMTXATTRS_UNSPECIFIED, bytes, sizeof(bytes));
    s->capture_in_flight = 0;
    if (result != MEMTX_OK) {
        s->capture_fault = true; s->diag_done = 0; s->capture_errors++;
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc-capture: RAM write failed, completion withheld\n");
        return;
    }
    s->capture_words++; s->diag_count++; s->diag_done_address = s->posted_address;
    if (s->posted_last) {
        s->diag_done = 1; s->capture_cycles++;
        if (s->capture_repeat) { s->diag_count = 0; }
        else { s->capture_finished = true; }
    }
    s->sample_remaining = s->sample_period_ns; ws53_adc_capture_resume(s);
}
static bool ws53_adc_scan_write(WS53AdcState *s, hwaddr off, uint32_t v)
{
    const uint32_t source_profile = (3u << 8) | (6u << 10) | (26u << 15) | (32u << 23);
    switch (off) {
    case 0:
        if (s->scan_running || (v & ~255u) != source_profile || !(v & 255)) { return false; }
        s->scan_control = v; return true;
    case 0x28:
        return !s->scan_running && v == 0; /* Only automatic channel selection. */
    case 0x1c:
        if (v == 2) { ws53_adc_capture_stop(s); return true; }
        if (v != 1 || s->scan_running || s->capture_in_flight || !s->cap_done || s->stage >= 0 ||
            s->offset_en || s->cap_en || !s->scan_control || !(s->diag_mode & 0x100) || s->capture_finished) { return false; }
        if (!s->assume_capture) {
            qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc-capture: unimplemented scan without assume-capture\n"); return true;
        }
        s->scan_running = true; s->next_channel = 0; s->sample_remaining = s->sample_period_ns;
        ws53_adc_capture_resume(s); return true;
    default: return false;
    }
}
static uint64_t ws53_adc_diag_read(void *opaque, hwaddr off, unsigned size)
{
    WS53AdcState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 0: return s->diag_monitor;
    case 4: return s->diag_save;
    case 8: return s->diag_trace;
    case 12: return s->diag_monitor_clock;
    case 0x100: return s->diag_select;
    case 0x104: return s->diag_mode;
    case 0x108: return s->diag_length & 0xffff;
    case 0x10c: return s->diag_length >> 16;
    case 0x110: return s->diag_start & 0xffff;
    case 0x114: return s->diag_start >> 16;
    case 0x118: return s->diag_end & 0xffff;
    case 0x11c: return s->diag_end >> 16;
    case 0x120: return s->diag_done_address & 0xffff;
    case 0x124: return s->diag_done_address >> 16;
    case 0x128: return s->diag_done;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc-capture: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size); return 0;
}
static void ws53_adc_diag_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53AdcState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (off == 0x104) {
        if (v & ~0x301u) { goto invalid; }
        if (v == s->diag_mode) { return; }
        if (!(v & 0x100)) {
            if (v && (s->capture_in_flight || s->scan_running)) { goto invalid; }
            timer_del(s->sample_timer);
            s->diag_mode = v;
            if (v & 0x200) { s->diag_done = s->diag_done_address = 0; }
            return;
        }
        if ((s->diag_mode & 0x100) || !(v & 0x200) || s->capture_in_flight || s->scan_running ||
            s->diag_monitor != 1 || s->diag_save != 1 || s->diag_trace ||
            s->diag_monitor_clock != 0x1ff || s->diag_select != 6 || !ws53_adc_capture_range(s)) { goto invalid; }
        if (!s->assume_capture) {
            qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc-capture: unimplemented arm without assume-capture\n"); return;
        }
        s->diag_mode = v; s->diag_count = s->diag_done = s->diag_done_address = 0;
        s->capture_fault = s->capture_finished = false; s->capture_repeat = v & 1; return;
    }
    if ((s->diag_mode & 0x100) || s->capture_in_flight || s->scan_running) { goto invalid; }
    switch (off) {
    case 0: if (v > 1) { goto invalid; } s->diag_monitor = v; return;
    case 4: if (v > 1) { goto invalid; } s->diag_save = v; return;
    case 8:
        if (v) { goto invalid; } /* Clearing inherited ownership only, no trace generator. */
        s->diag_trace = 0; return;
    case 12: if (v & ~0x1ffu) { goto invalid; } s->diag_monitor_clock = v; return;
    case 0x100: if (v != 6) { goto invalid; } s->diag_select = v; return;
    default: break;
    }
    if (v > 0xffff) { goto invalid; }
    switch (off) {
    case 0x108: s->diag_length = (s->diag_length & 0xffff0000u) | v; return;
    case 0x10c: s->diag_length = (s->diag_length & 0xffffu) | (v << 16); return;
    case 0x110: s->diag_start = (s->diag_start & 0xffff0000u) | v; return;
    case 0x114: s->diag_start = (s->diag_start & 0xffffu) | (v << 16); return;
    case 0x118: s->diag_end = (s->diag_end & 0xffff0000u) | v; return;
    case 0x11c: s->diag_end = (s->diag_end & 0xffffu) | (v << 16); return;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-adc-capture: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
}
static const MemoryRegionOps ws53_adc_diag_ops = {
    .read=ws53_adc_diag_read,.write=ws53_adc_diag_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_adc_capture_reset(WS53AdcState *s)
{
    timer_del(s->sample_timer); timer_del(s->write_timer);
    s->scan_running = s->capture_fault = s->capture_finished = s->capture_repeat = false;
    s->scan_control = s->diag_monitor = s->diag_save = s->diag_monitor_clock = s->diag_select = s->diag_mode = 0;
    s->diag_trace = s->initial_trace;
    s->diag_length = s->diag_start = s->diag_end = s->diag_done_address = s->diag_done = s->diag_count = 0;
    s->capture_in_flight = s->capture_words = s->capture_cycles = s->capture_errors = 0;
    /* External input voltages/codes and clock/fault inputs survive reset. */
}
#define WS53_ADC_CAPTURE_INPUT(name, field) \
static void ws53_adc_capture_##name(void *opaque, int n, int level) { \
    WS53AdcState *s = opaque; ws53_adc_capture_pause(s); s->field = !!level; ws53_adc_capture_resume(s); }
WS53_ADC_CAPTURE_INPUT(clock, diag_clock)
WS53_ADC_CAPTURE_INPUT(sample_stall, sample_stall)
WS53_ADC_CAPTURE_INPUT(bus_stall, bus_stall)
WS53_ADC_CAPTURE_INPUT(bus_error, bus_error)
WS53_ADC_CAPTURE_INPUT(early_done, early_done)
#undef WS53_ADC_CAPTURE_INPUT
static void ws53_adc_capture_input(void *opaque, int n, int level)
{
    WS53AdcState *s = opaque;
    s->sample_input[n / 12] = (s->sample_input[n / 12] & ~(1u << (n % 12))) | (!!level << (n % 12));
}
static void ws53_adc_capture_init(Object *obj)
{
    WS53AdcState *s = WS53_ADC(obj); s->diag_clock = true;
    for (unsigned ch = 0; ch < 8; ch++) { s->sample_input[ch] = 100 + 100 * ch; }
    memory_region_init_io(&s->diag_mmio, obj, &ws53_adc_diag_ops, s, "ws53-adc-capture", 0x12c);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->diag_mmio);
    s->sample_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_adc_capture_sample, s);
    s->write_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_adc_capture_writeback, s);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_capture_clock, "diag-clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_capture_sample_stall, "sample-stall", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_capture_bus_stall, "bus-stall", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_capture_bus_error, "bus-error", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_capture_early_done, "early-done", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_adc_capture_input, "input", 96);
    object_property_add_uint64_ptr(obj, "capture-in-flight", &s->capture_in_flight, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "capture-words", &s->capture_words, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "capture-cycles", &s->capture_cycles, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "capture-errors", &s->capture_errors, OBJ_PROP_FLAG_READ);
}
