/* WS53 PWM v151, SDK163d9150/RDL f188fd4. Explicit assumed digital profile:
 * Configurable effective counter rate, low16 counters, immediate/deferred commit.
 * 32MHz is the SDK report; A-board bounded RAM handoff measures 2MHz.
 * The measured profile supports local divisors1/2/4/8/16 with an effective
 * 32MHz input. Upstream source identity and other encodings remain unmodeled.
 * These are channel outputs, not an invented physical pinmux/electrical model.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_PWM "ws53-pwm"
OBJECT_DECLARE_SIMPLE_TYPE(WS53PwmState, WS53_PWM)
#define WS53_PWM_SECOND 1000000000ULL
enum { PW_EN, PW_POL, PW_OE, PW_OFFSET_L, PW_OFFSET_H, PW_FREQ_L,
       PW_FREQ_H, PW_DUTY_L, PW_DUTY_H, PW_FLAG, PW_CYCLES, PW_COUNT };
typedef struct WS53PwmChannel {
    uint16_t shadow[12], active[12], next[12];
    uint32_t phase, count, fraction;
    int64_t last_ns;
    bool queued, running, flag;
} WS53PwmChannel;
struct WS53PwmState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, div_mmio;
    QEMUTimer *timer, *commit_timer;
    int64_t commit_due[4];
    uint8_t commit_pending;
    uint32_t start_latch_ns;
    qemu_irq irq[2], out[8], oe[8];
    WS53PwmChannel channel[8];
    uint8_t select[4], clear[4], event_enable;
    uint16_t group_errors, count_errors, gate, div, latched_div;
    bool assume_timing, diagnosed, external_clock;
    uint64_t counter_hz; /* reference effective rate at divisor16 */
};

/* Only the measured A-board profile qualifies divisors other than16. */
static bool ws53_pwm_divisor_valid(WS53PwmState *s, unsigned divisor)
{
    return divisor == 16 || (s->counter_hz == 2000000 &&
           (divisor == 1 || divisor == 2 || divisor == 4 || divisor == 8));
}
static uint64_t ws53_pwm_rate(WS53PwmState *s)
{
    return s->counter_hz * 16 / s->latched_div;
}
static bool ws53_pwm_clocked(WS53PwmState *s)
{
    return s->external_clock && (s->gate & 0x80) && (s->div & 1) && ws53_pwm_divisor_valid(s, s->latched_div);
}
static void ws53_pwm_irq(WS53PwmState *s)
{
    bool end = false;
    for (unsigned i = 0; i < 8; i++) { end |= s->channel[i].flag; }
    qemu_set_irq(s->irq[0], (s->event_enable & 1) && (s->group_errors || s->count_errors));
    qemu_set_irq(s->irq[1], (s->event_enable & 2) && end);
}
static void ws53_pwm_output(WS53PwmState *s, unsigned i)
{
    WS53PwmChannel *c = &s->channel[i];
    bool active = c->running && c->phase >= c->active[PW_OFFSET_L] &&
                  c->phase < c->active[PW_OFFSET_L] + c->active[PW_DUTY_L];
    bool drive = !c->active[PW_OE] || active;
    bool level = c->running && (active ^ !!c->active[PW_POL]);
    qemu_set_irq(s->oe[i], drive);
    qemu_set_irq(s->out[i], drive && level);
}
static bool ws53_pwm_profile(WS53PwmState *s)
{
    if (!s->assume_timing) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented timing without assume-timing profile\n");
        return false;
    }
    if (!s->diagnosed) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-pwm: assumed timing clock=%" PRIu64 " width=16 clear=immediate deferred=block-end\n",
            s->counter_hz);
        if (s->start_latch_ns) {
            qemu_log_mask(LOG_GUEST_ERROR,
                "ws53-pwm: assumed START acceptance delay=%uns; live configuration sampled at acceptance\n",
                s->start_latch_ns);
        }
        s->diagnosed = true;
    }
    return true;
}
static void ws53_pwm_load(WS53PwmChannel *c, const uint16_t *config, int64_t now)
{
    memcpy(c->active, config, sizeof(c->active));
    c->running = config[PW_EN];
    c->phase = c->count = c->fraction = 0;
    c->last_ns = now;
}
static void ws53_pwm_sync(WS53PwmState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    for (unsigned i = 0; i < 8; i++) {
        WS53PwmChannel *c = &s->channel[i];
        uint64_t elapsed = now - c->last_ns;
        c->last_ns = now;
        if (!c->running || !ws53_pwm_clocked(s)) { continue; }
        uint64_t hz = ws53_pwm_rate(s);
        uint64_t part = (elapsed % WS53_PWM_SECOND) * hz + c->fraction;
        uint64_t ticks = (elapsed / WS53_PWM_SECOND) * hz + part / WS53_PWM_SECOND;
        c->fraction = part % WS53_PWM_SECOND;
        while (c->running && ticks) {
            uint64_t period = c->active[PW_FREQ_L];
            uint64_t block = c->active[PW_CYCLES] ? c->active[PW_CYCLES] : 1;
            uint64_t to_end = (block - (c->count % block)) * period - c->phase;
            if (ticks < to_end) {
                uint64_t total = c->phase + ticks;
                c->count = (c->count + total / period) & 0xffff;
                c->phase = total % period;
                break;
            }
            ticks -= to_end;
            c->phase = 0;
            c->count = c->active[PW_CYCLES] ? 0 : (c->count + 1) & 0xffff;
            if (c->active[PW_CYCLES]) { c->flag = true; }
            if (c->queued) {
                uint32_t fraction = c->fraction;
                ws53_pwm_load(c, c->next, now);
                c->fraction = fraction;
                c->queued = false;
            } else if (ticks >= block * period) {
                /* Skip complete identical blocks when a large qtest step or
                 * delayed callback catches up. No unbounded per-cycle loop. */
                uint64_t blocks = ticks / (block * period);
                if (!c->active[PW_CYCLES]) { c->count = (c->count + blocks) & 0xffff; }
                ticks %= block * period;
            }
        }
        ws53_pwm_output(s, i);
    }
    ws53_pwm_irq(s);
}
static void ws53_pwm_arm(WS53PwmState *s)
{
    timer_del(s->timer);
    if (!ws53_pwm_clocked(s)) { return; }
    uint64_t next = UINT64_MAX;
    for (unsigned i = 0; i < 8; i++) {
        WS53PwmChannel *c = &s->channel[i];
        if (!c->running) { continue; }
        uint64_t edge = c->active[PW_FREQ_L];
        uint32_t start = c->active[PW_OFFSET_L];
        uint32_t end = start + c->active[PW_DUTY_L];
        if (c->phase < start) { edge = start; }
        else if (c->phase < end) { edge = end; }
        uint64_t numerator = (edge - c->phase) * WS53_PWM_SECOND - c->fraction;
        uint64_t hz = ws53_pwm_rate(s);
        next = MIN(next, (numerator + hz - 1) / hz);
    }
    if (next != UINT64_MAX) {
        timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + next);
    }
}
static void ws53_pwm_tick(void *opaque)
{
    WS53PwmState *s = opaque;
    ws53_pwm_sync(s); ws53_pwm_arm(s);
}
static void ws53_pwm_commit(WS53PwmState *s, unsigned group)
{
    if (!ws53_pwm_profile(s)) { return; }
    ws53_pwm_sync(s);
    uint8_t mask = s->select[group], conflicts = 0, invalid = 0;
    for (unsigned g = 0; g < 4; g++) { if (g != group) { conflicts |= mask & s->select[g]; } }
    for (unsigned i = 0; i < 8; i++) {
        uint16_t *v = s->channel[i].shadow;
        if (!(mask & (1u << i))) { continue; }
        if (v[PW_EN] && (!v[PW_FREQ_L] || v[PW_DUTY_L] > v[PW_FREQ_L] ||
                         v[PW_OFFSET_L] > v[PW_FREQ_L] - v[PW_DUTY_L])) { invalid |= 1u << i; }
    }
    s->group_errors |= conflicts; s->count_errors |= invalid;
    if (conflicts || invalid) { ws53_pwm_irq(s); return; }
    for (unsigned i = 0; i < 8; i++) {
        if (!(mask & (1u << i))) { continue; }
        WS53PwmChannel *c = &s->channel[i];
        if (s->clear[group] || !c->running) {
            ws53_pwm_load(c, c->shadow, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
            /* Measured finite-mode initial load, before block completion.
             * Continuous-mode initial notification remains unqualified. */
            if (ws53_pwm_clocked(s) && c->running && c->active[PW_CYCLES]) {
                c->flag = true;
            }
            c->queued = false;
        } else {
            memcpy(c->next, c->shadow, sizeof(c->next)); c->queued = true;
        }
        ws53_pwm_output(s, i);
    }
    ws53_pwm_arm(s); ws53_pwm_irq(s);
}
static void ws53_pwm_commit_arm(WS53PwmState *s)
{
    int64_t next = INT64_MAX;
    timer_del(s->commit_timer);
    for (unsigned g = 0; g < 4; g++) {
        if (s->commit_pending & (1u << g)) { next = MIN(next, s->commit_due[g]); }
    }
    if (next != INT64_MAX) { timer_mod(s->commit_timer, next); }
}
static void ws53_pwm_accept(void *opaque)
{
    WS53PwmState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    for (unsigned g = 0; g < 4; g++) {
        if ((s->commit_pending & (1u << g)) && s->commit_due[g] <= now) {
            s->commit_pending &= ~(1u << g);
            if (!ws53_pwm_clocked(s)) {
                qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented START acceptance while clock gated\n");
                continue;
            }
            ws53_pwm_commit(s, g);
        }
    }
    ws53_pwm_commit_arm(s);
}
static void ws53_pwm_request_commit(WS53PwmState *s, unsigned group)
{
    if (!s->start_latch_ns) { ws53_pwm_commit(s, group); return; }
    if (!ws53_pwm_profile(s)) { return; }
    if (s->commit_pending & (1u << group)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented overlapping START group%u\n", group);
        return;
    }
    s->commit_pending |= 1u << group;
    s->commit_due[group] = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->start_latch_ns;
    ws53_pwm_commit_arm(s);
}
static uint64_t ws53_pwm_read(void *opaque, hwaddr off, unsigned size)
{
    WS53PwmState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    ws53_pwm_sync(s);
    if (off < 0x40 && (off & 15) < 8) {
        return (off & 4) ? s->clear[off / 16] : s->select[off / 16];
    }
    if (off >= 0x100 && off < 0x300 && (off & 63) < 48) {
        WS53PwmChannel *c = &s->channel[(off - 0x100) / 64];
        unsigned reg = (off & 63) / 4;
        if (reg == PW_FLAG) { return c->flag; }
        if (reg == PW_COUNT) {
            /* Finite pulse trains expose the current1-based pulse index,
             * including the first partial period; deferred stop reads0.
             * Continuous-mode phase remains the earlier explicit assumption. */
            return c->running && c->active[PW_CYCLES] ? c->count + 1 : c->count;
        }
        return c->shadow[reg];
    }
    switch (off) {
    case 0x500: return s->group_errors;
    case 0x504: return s->count_errors;
    case 0x510: return s->event_enable;
    case 0x514: return 0; /* No enabled DMA support in this model. */
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_pwm_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53PwmState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    ws53_pwm_sync(s);
    if (off < 0x40) {
        unsigned g = off / 16;
        switch (off & 15) {
        case 0: if (v > 255) { goto invalid; } s->select[g] = v; return;
        case 4: if (v > 1) { goto invalid; } s->clear[g] = v; return;
        case 8: if (v != 1) { goto invalid; } ws53_pwm_request_commit(s, g); return;
        }
    }
    if (off >= 0x100 && off < 0x300 && (off & 63) < 48) {
        WS53PwmChannel *c = &s->channel[(off - 0x100) / 64];
        unsigned reg = (off & 63) / 4;
        if (reg == PW_FLAG || reg == PW_COUNT || v > (reg < 3 ? 1 : 0xffff)) { goto invalid; }
        /* High-word nonzero counting is deliberately not guessed. */
        if ((reg == PW_OFFSET_H || reg == PW_FREQ_H || reg == PW_DUTY_H) && v) { goto invalid; }
        c->shadow[reg] = v; return;
    }
    switch (off) {
    case 0x508: if (v > 0xffff) { goto invalid; } s->group_errors &= ~v; break;
    case 0x50c: if (v > 0xffff) { goto invalid; } s->count_errors &= ~v; break;
    case 0x510: if (v > 3) { goto invalid; } s->event_enable = v; break;
    case 0x514: if (v) { goto invalid; } return;
    case 0x518:
        if (v > 255) { goto invalid; }
        for (unsigned i = 0; i < 8; i++) { if (v & (1u << i)) { s->channel[i].flag = false; } }
        break;
    default: goto invalid;
    }
    ws53_pwm_irq(s); return;
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
}
static uint64_t ws53_pwm_div_read(void *opaque, hwaddr off, unsigned size)
{
    WS53PwmState *s = opaque;
    if (!off && size == 2) { return s->div; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented divider read size=%u\n", size); return 0;
}
static void ws53_pwm_gate_input(void *opaque, int n, int level)
{
    WS53PwmState *s = opaque;
    ws53_pwm_sync(s); s->gate = level ? 0x80 : 0; ws53_pwm_arm(s);
}
static void ws53_pwm_div_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53PwmState *s = opaque;
    if (off || size != 2 || (v & ~0x7fu) || ((v & 2) && !(s->div & 2) && !ws53_pwm_divisor_valid(s, (v >> 2) & 31))) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-pwm: unimplemented divider write value=0x%" PRIx64 " size=%u\n", v, size); return;
    }
    ws53_pwm_sync(s);
    if ((v & 2) && !(s->div & 2)) { s->latched_div = (v >> 2) & 31; }
    s->div = v; ws53_pwm_arm(s);
}
#define WS53_PWM_OPS(name, reader, writer) \
    static const MemoryRegionOps name = { .read=reader, .write=writer, \
        .endianness=DEVICE_LITTLE_ENDIAN, \
        .valid={.min_access_size=1,.max_access_size=8,.unaligned=true}, \
        .impl={.min_access_size=1,.max_access_size=8,.unaligned=true} }
WS53_PWM_OPS(ws53_pwm_ops, ws53_pwm_read, ws53_pwm_write);
WS53_PWM_OPS(ws53_pwm_div_ops, ws53_pwm_div_read, ws53_pwm_div_write);
#undef WS53_PWM_OPS
static void ws53_pwm_clock_input(void *opaque, int n, int level)
{
    WS53PwmState *s = opaque;
    ws53_pwm_sync(s); s->external_clock = level; ws53_pwm_arm(s);
}
static void ws53_pwm_reset(void *opaque)
{
    WS53PwmState *s = opaque;
    timer_del(s->timer); timer_del(s->commit_timer); s->commit_pending = 0;
    memset(s->channel, 0, sizeof(s->channel));
    memset(s->select, 0, sizeof(s->select)); memset(s->clear, 0, sizeof(s->clear));
    s->event_enable = s->group_errors = s->count_errors = s->div = s->latched_div = 0;
    for (unsigned i = 0; i < 8; i++) {
        s->channel[i].last_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL); ws53_pwm_output(s, i);
    }
    ws53_pwm_irq(s); /* External clock fault and assumption notice survive reset. */
}
static void ws53_pwm_realize(DeviceState *dev, Error **errp)
{
    WS53PwmState *s = WS53_PWM(dev);
    if (s->counter_hz != 2000000 && s->counter_hz != 32000000) {
        error_setg(errp, "ws53-pwm: counter-hz must select the 2MHz HIL or 32MHz SDK profile");
        return;
    }
    if (s->start_latch_ns != 0 && s->start_latch_ns != 1000) {
        error_setg(errp, "ws53-pwm: start-latch-ns must be 0 or the explicit 1000ns test profile");
        return;
    }
    qemu_register_reset(ws53_pwm_reset, s);
}
static const Property ws53_pwm_properties[] = {
    DEFINE_PROP_UINT32("start-latch-ns", WS53PwmState, start_latch_ns, 0),
    DEFINE_PROP_BOOL("assume-timing", WS53PwmState, assume_timing, false),
    DEFINE_PROP_UINT64("counter-hz", WS53PwmState, counter_hz, 32000000),
};
static void ws53_pwm_init(Object *obj)
{
    WS53PwmState *s = WS53_PWM(obj);
    s->external_clock = true;
    memory_region_init_io(&s->mmio, obj, &ws53_pwm_ops, s, TYPE_WS53_PWM, 0x51c);
    memory_region_init_io(&s->div_mmio, obj, &ws53_pwm_div_ops, s, "ws53-pwm-divider", 2);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->div_mmio);
    for (unsigned i = 0; i < 2; i++) { sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq[i]); }
    qdev_init_gpio_out_named(DEVICE(obj), s->out, "out", 8);
    qdev_init_gpio_out_named(DEVICE(obj), s->oe, "output-enable", 8);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pwm_clock_input, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_pwm_gate_input, "gate", 1);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_pwm_tick, s);
    s->commit_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_pwm_accept, s);
}
static void ws53_pwm_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_pwm_realize; device_class_set_props(dc, ws53_pwm_properties);
}
static const TypeInfo ws53_pwm_type = {
    .name=TYPE_WS53_PWM,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53PwmState),
    .instance_init=ws53_pwm_init,.class_init=ws53_pwm_class_init,
};
