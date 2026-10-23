/* WS53 I2S v151, SDK 163d9150 / RDL 471ef7c.
 * Explicit assumed digital stereo profile; no physical pins or silicon timing
 * claim. Four FIFO8 queues and a distinct serializer, 32MHz master source,
 * deterministic virtual stereo peer, phase-preserving gates. See model tests.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_I2S "ws53-i2s"
OBJECT_DECLARE_SIMPLE_TYPE(WS53I2sState, WS53_I2S)
#define I2S_TX (1u << 12)
#define I2S_RX (1u << 13)
#define I2S_IRQ (1u << 14)
#define I2S_RSTN (1u << 15)
typedef struct WS53I2sFifo {
    uint32_t words[8];
    unsigned head, count;
} WS53I2sFifo;
struct WS53I2sState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, cmu_mmio;
    QEMUTimer *timer;
    qemu_irq irq;
    WS53I2sFifo tx[2], rx[2];
    uint32_t mode, width, threshold, mask, control, crg, div, fs, ratio, cmu;
    uint32_t errors, pending[2];
    bool assume_profile, gate, clock_on, slave_clock, stall, reset_stall;
    bool active;
    int64_t remaining, deadline;
    uint64_t slave_period_ns, frames, transmitted, received, in_flight;
    uint64_t last_left, last_right;
};
static unsigned ws53_i2s_width(WS53I2sState *s)
{
    static const unsigned bits[] = {0, 16, 18, 20, 24, 32};
    unsigned code = s->width & 7;
    return code < ARRAY_SIZE(bits) && code == (s->width >> 3) ? bits[code] : 0;
}
static uint32_t ws53_i2s_sample_mask(WS53I2sState *s)
{
    unsigned bits = ws53_i2s_width(s);
    return bits == 32 ? UINT32_MAX : (1u << bits) - 1;
}
static uint32_t ws53_i2s_status(WS53I2sState *s)
{
    uint32_t status = s->errors;
    if ((s->control & I2S_RX) && MIN(s->rx[0].count, s->rx[1].count) > (s->threshold >> 8)) {
        status |= 1;
    }
    if ((s->control & I2S_TX) && MAX(s->tx[0].count, s->tx[1].count) <= (s->threshold & 255)) {
        status |= 2;
    }
    return status;
}
static void ws53_i2s_irq(WS53I2sState *s)
{
    qemu_set_irq(s->irq, (s->control & I2S_IRQ) && (ws53_i2s_status(s) & ~s->mask));
}
static uint32_t ws53_i2s_pop(WS53I2sFifo *q)
{
    uint32_t v = q->words[q->head];
    q->head = (q->head + 1) % 8; q->count--;
    return v;
}
static void ws53_i2s_push(WS53I2sFifo *q, uint32_t v)
{
    q->words[(q->head + q->count) % 8] = v; q->count++;
}
static bool ws53_i2s_clocked(WS53I2sState *s)
{
    return s->clock_on && !s->stall && s->gate && (s->cmu & 1) &&
        (s->crg & 2) && ((s->mode & 128) ? (s->crg & 1) : s->slave_clock);
}
static bool ws53_i2s_enabled(WS53I2sState *s)
{
    return (s->control & I2S_RSTN) && (s->control & (I2S_TX | I2S_RX));
}
static bool ws53_i2s_configured(WS53I2sState *s)
{
    unsigned width = ws53_i2s_width(s);
    return width && (!(s->mode & 128) ||
        (s->div && s->fs == width * 2 && s->ratio == width && (s->crg & 1)));
}
static void ws53_i2s_pause(WS53I2sState *s)
{
    if (timer_pending(s->timer)) {
        s->remaining = MAX(s->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(s->timer);
    }
}
static void ws53_i2s_cancel(WS53I2sState *s)
{
    timer_del(s->timer); s->active = false; s->in_flight = 0;
}
static void ws53_i2s_resume(WS53I2sState *s)
{
    if (!ws53_i2s_enabled(s) || !ws53_i2s_clocked(s) || timer_pending(s->timer)) { return; }
    if (!s->active) {
        s->active = true;
        s->in_flight = 1;
        s->remaining = (s->mode & 128) ?
            ((uint64_t)s->div * s->fs * 1000000000ULL + 31999999) / 32000000 : s->slave_period_ns;
        for (unsigned i = 0; i < 2; i++) {
            s->pending[i] = 0;
            if (s->control & I2S_TX) {
                if (s->tx[i].count) { s->pending[i] = ws53_i2s_pop(&s->tx[i]); }
                else { s->errors |= i ? 16 : 32; }
            }
        }
        ws53_i2s_irq(s);
    }
    s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining;
    timer_mod(s->timer, s->deadline);
}
static void ws53_i2s_tick(void *opaque)
{
    WS53I2sState *s = opaque;
    s->frames++;
    if (s->control & I2S_TX) {
        s->transmitted++; s->last_left = s->pending[0]; s->last_right = s->pending[1];
    }
    if (s->control & I2S_RX) {
        for (unsigned i = 0; i < 2; i++) {
            uint32_t v = (s->control & I2S_TX) ? s->pending[i] ^ (i ? 0x5a5a : 0xa5a5) :
                (i ? ~(uint32_t)s->frames : (uint32_t)s->frames);
            if (s->rx[i].count == 8) { s->errors |= i ? 4 : 8; }
            else { ws53_i2s_push(&s->rx[i], v & ws53_i2s_sample_mask(s)); }
        }
        s->received++;
    }
    s->active = false; s->in_flight = 0;
    ws53_i2s_irq(s); ws53_i2s_resume(s);
}
static uint64_t ws53_i2s_read(void *opaque, hwaddr off, unsigned size)
{
    WS53I2sState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 0x3c: return 0; /* Model version, not a claimed silicon VERSION. */
    case 0x40: return s->mode;
    case 0x44: return ws53_i2s_status(s);
    case 0x54: case 0x58: {
        WS53I2sFifo *q = &s->rx[off == 0x58];
        if (!q->count) { goto invalid; }
        uint32_t v = ws53_i2s_pop(q); ws53_i2s_irq(s); return v;
    }
    case 0x64: return s->threshold;
    case 0x68: return s->rx[0].count | (s->rx[1].count << 8);
    case 0x6c: return s->tx[0].count | (s->tx[1].count << 8);
    case 0x78: return s->width;
    case 0x7c: case 0x80: case 0x84: case 0x88: return 0;
    case 0x8c: return s->mask;
    case 0x90: return s->crg;
    case 0x94: return s->div;
    case 0x98: return s->fs;
    case 0x9c: return s->ratio;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2s: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_i2s_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53I2sState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 0x48:
        if (v & ~63u) { goto invalid; }
        s->errors &= ~v; ws53_i2s_irq(s); return;
    case 0x4c: case 0x50: {
        WS53I2sFifo *q = &s->tx[off == 0x50];
        if (!(s->control & I2S_RSTN) || !ws53_i2s_width(s) ||
            q->count == 8 || (v & ~ws53_i2s_sample_mask(s))) { goto invalid; }
        ws53_i2s_push(q, v); ws53_i2s_irq(s); return;
    }
    case 0x5c: case 0x60: {
        if ((v & ~0xff00u) || (off == 0x5c && (v & 0xf00))) { goto invalid; }
        uint32_t next = off == 0x5c ? s->control | v : s->control & ~v;
        if (off == 0x5c && (v & (I2S_TX | I2S_RX))) {
            if (!s->assume_profile) {
                qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2s: unimplemented transfer without assume-profile\n"); return;
            }
            if (!(next & I2S_RSTN) || !ws53_i2s_configured(s)) { goto invalid; }
        }
        /* Changing directions cancels a partial serializer frame explicitly;
         * no claim that a disable command safely drains physical outputs. */
        if ((next ^ s->control) & (I2S_TX | I2S_RX | I2S_RSTN)) { ws53_i2s_cancel(s); }
        s->control = next;
        if (!(next & I2S_RSTN)) {
            s->control &= ~(I2S_TX | I2S_RX);
            if (!s->reset_stall) {
                memset(s->tx, 0, sizeof(s->tx)); memset(s->rx, 0, sizeof(s->rx)); s->errors = 0;
            }
        }
        ws53_i2s_irq(s); ws53_i2s_resume(s); return;
    }
    case 0x8c:
        if (v & ~63u) { goto invalid; }
        s->mask = v; ws53_i2s_irq(s); return;
    case 0x90:
        if ((v & ~3u) || (ws53_i2s_enabled(s) && ((v ^ s->crg) & ~2u))) { goto invalid; }
        ws53_i2s_pause(s); s->crg = v; ws53_i2s_resume(s); return;
    default: break;
    }
    if (ws53_i2s_enabled(s)) { goto invalid; }
    switch (off) {
    case 0x3c: case 0x7c: case 0x80: case 0x84: case 0x88:
        if (v) { goto invalid; } return;
    case 0x40:
        if (v & ~0xc0u) { goto invalid; } s->mode = v; return;
    case 0x64:
        if ((v & ~0x707u)) { goto invalid; } s->threshold = v; return;
    case 0x78:
        if ((v & ~0x3fu) || !(v & 7) || (v & 7) > 5 || (v & 7) != (v >> 3)) { goto invalid; }
        s->width = v; return;
    case 0x94:
        if (!v || v > 127) { goto invalid; } s->div = v; return;
    case 0x98:
        if (v > 1023) { goto invalid; } s->fs = v; return;
    case 0x9c:
        if (v > 2047) { goto invalid; } s->ratio = v; return;
    default: break;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2s: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
}
static uint64_t ws53_i2s_cmu_read(void *opaque, hwaddr off, unsigned size)
{
    WS53I2sState *s = opaque;
    if (!off && size == 4) { return s->cmu; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2s: unimplemented CMU read size=%u\n", size); return 0;
}
static void ws53_i2s_cmu_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53I2sState *s = opaque;
    if (off || size != 4 || (v & ~1u)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2s: unimplemented CMU write size=%u value=0x%" PRIx64 "\n", size, v); return;
    }
    ws53_i2s_pause(s); s->cmu = v; ws53_i2s_resume(s);
}
static const MemoryRegionOps ws53_i2s_ops = {
    .read=ws53_i2s_read,.write=ws53_i2s_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static const MemoryRegionOps ws53_i2s_cmu_ops = {
    .read=ws53_i2s_cmu_read,.write=ws53_i2s_cmu_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
#define WS53_I2S_INPUT(name, field) \
static void ws53_i2s_##name(void *opaque, int n, int level) \
{ \
    WS53I2sState *s = opaque; \
    ws53_i2s_pause(s); s->field = !!level; ws53_i2s_resume(s); \
}
WS53_I2S_INPUT(gate_input, gate)
WS53_I2S_INPUT(clock_input, clock_on)
WS53_I2S_INPUT(slave_input, slave_clock)
WS53_I2S_INPUT(stall_input, stall)
WS53_I2S_INPUT(reset_stall_input, reset_stall)
#undef WS53_I2S_INPUT
static void ws53_i2s_reset(void *opaque)
{
    WS53I2sState *s = opaque;
    ws53_i2s_cancel(s);
    memset(s->tx, 0, sizeof(s->tx)); memset(s->rx, 0, sizeof(s->rx));
    s->mode = s->width = s->threshold = s->mask = s->control = s->crg = 0;
    s->div = s->fs = s->ratio = s->cmu = s->errors = 0;
    s->frames = s->transmitted = s->received = s->last_left = s->last_right = 0;
    /* Faults/external clocks survive; these zeros are model reset policy. */
    ws53_i2s_irq(s);
}
static void ws53_i2s_realize(DeviceState *dev, Error **errp)
{
    WS53I2sState *s = WS53_I2S(dev);
    if (!s->slave_period_ns || s->slave_period_ns > 1000000000) {
        error_setg(errp, "ws53-i2s: slave-period-ns must be in 1..1000000000"); return;
    }
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2s: assumed stereo profile FIFO8 master32MHz separate-serializer XOR-peer\n");
    }
    qemu_register_reset(ws53_i2s_reset, s);
}
static const Property ws53_i2s_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53I2sState, assume_profile, false),
    DEFINE_PROP_UINT64("slave-period-ns", WS53I2sState, slave_period_ns, 100000),
};
static void ws53_i2s_init(Object *obj)
{
    WS53I2sState *s = WS53_I2S(obj);
    s->clock_on = s->slave_clock = true;
    memory_region_init_io(&s->mmio, obj, &ws53_i2s_ops, s, TYPE_WS53_I2S, 0xc4);
    memory_region_init_io(&s->cmu_mmio, obj, &ws53_i2s_cmu_ops, s, "ws53-audio-cmu", 4);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->cmu_mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_i2s_tick, s);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2s_gate_input, "gate", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2s_clock_input, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2s_slave_input, "slave-clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2s_stall_input, "stall", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2s_reset_stall_input, "reset-stall", 1);
    object_property_add_uint64_ptr(obj, "frames", &s->frames, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "transmitted", &s->transmitted, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "received", &s->received, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "in-flight", &s->in_flight, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "last-left", &s->last_left, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "last-right", &s->last_right, OBJ_PROP_FLAG_READ);
}
static void ws53_i2s_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_i2s_realize;
    device_class_set_props(dc, ws53_i2s_properties);
}
static const TypeInfo ws53_i2s_type = {
    .name=TYPE_WS53_I2S,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53I2sState),
    .instance_init=ws53_i2s_init,.class_init=ws53_i2s_class_init,
};
