/* WS53 v151_100 SPI0 and QSPI1 single-line functional profile.
 * Register facts: hisi-registers 04ced9c, fbb_ws53 163d9150.
 * FIFO8, 32MHz, 1us enable crossing and XOR peer are explicit test assumptions.
 * No native CS/electrical/XIP/dual/quad/DMA behavior is claimed.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_SPI "ws53-spi"
OBJECT_DECLARE_SIMPLE_TYPE(WS53SpiState, WS53_SPI)
enum WS53SpiReg {
    SP_ENABLE, SP_CONTROL, SP_COUNT, SP_ENHANCED, SP_MICROWIRE, SP_BAUD,
    SP_DMA, SP_DMA_RX, SP_DMA_TX, SP_SAMPLE, SP_EDGE, SP_RAW, SP_MASKED,
    SP_MASK, SP_SELECT, SP_TX_THRESHOLD, SP_TX_LEVEL, SP_CLEAR_TX,
    SP_RX_THRESHOLD, SP_RX_LEVEL, SP_CLEAR_UNDER, SP_STATUS, SP_CLEAR_MULTI,
    SP_VERSION, SP_CLEAR_OVER, SP_CLEAR_ALL, SP_ID, SP_REG_COUNT,
};
/* Same names are NOT same layouts. The FIFO windows also differ. */
static const uint16_t ws53_spi_offsets[2][SP_REG_COUNT] = {
    {0,4,8,12,16,20,24,28,32,36,40,188,192,196,200,204,208,212,
     216,220,224,228,232,236,244,248,0xffff},
    {8,0,4,244,12,20,76,84,80,240,248,52,48,44,16,24,32,56,
     28,36,64,40,68,92,60,72,88},
};
/* Canonical interrupt bits follow QSPI ordering, translated at the MMIO edge. */
enum { SP_TX_EMPTY=1, SP_TX_OVER=2, SP_RX_UNDER=4, SP_RX_OVER=8, SP_RX_FULL=16, SP_MULTI=32 };
typedef struct WS53SpiChannel {
    WS53SpiState *owner;
    unsigned index;
    MemoryRegion mmio;
    QEMUTimer *timer;
    qemu_irq irq;
    uint32_t regs[SP_REG_COUNT], tx[8], rx[8], latched;
    unsigned tx_head, tx_len, rx_head, rx_len;
    bool enabled, scheduled, enable_event, clock, stretch, collision, disable_stall;
    int64_t remaining, deadline;
    uint64_t frames;
} WS53SpiChannel;
typedef struct WS53SpiGate {
    WS53SpiState *owner;
    unsigned index;
    MemoryRegion mmio;
    uint16_t value;
} WS53SpiGate;
struct WS53SpiState {
    SysBusDevice parent_obj;
    WS53SpiChannel channel[2];
    /* SPI_CLOCK, SPI_DEVICE_MODE, GLOBAL_CLKEN, XIP_QSPI_CLOCK, XIP_CLOCK_ENABLE */
    WS53SpiGate gate[5];
    bool spi_gate, assume_profile;
};
static uint32_t ws53_spi_encode(WS53SpiChannel *c, uint32_t v)
{
    if (c->index) { return v; }
    return ((v & 1) << 7) | ((v & 2) << 7) | (v & 4) |
        ((v & 8) >> 2) | ((v & 16) >> 4) | ((v & 32) << 10);
}
static uint32_t ws53_spi_raw(WS53SpiChannel *c)
{
    return c->latched | (c->rx_len > c->regs[SP_RX_THRESHOLD] ? SP_RX_FULL : 0) |
        (c->enabled && c->tx_len <= c->regs[SP_TX_THRESHOLD] ? SP_TX_EMPTY : 0);
}
static void ws53_spi_irq(WS53SpiChannel *c)
{
    qemu_set_irq(c->irq, !!(ws53_spi_encode(c, ws53_spi_raw(c)) & c->regs[SP_MASK]));
}
static bool ws53_spi_clocked(WS53SpiChannel *c)
{
    WS53SpiState *s = c->owner;
    if (!s->assume_profile || !c->clock) { return false; }
    return c->index ? (s->gate[2].value & 0x80) &&
        (s->gate[3].value & 0x1ff) == 1 && (s->gate[4].value & 0x2a) == 0x2a :
        s->spi_gate && (s->gate[0].value & 1);
}
static void ws53_spi_pause(WS53SpiChannel *c)
{
    if (c->scheduled && timer_pending(c->timer)) {
        c->remaining = MAX(c->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(c->timer);
    }
}
static void ws53_spi_resume(WS53SpiChannel *c)
{
    if (c->scheduled && !timer_pending(c->timer) && ws53_spi_clocked(c) &&
        (c->enable_event ? (c->regs[SP_ENABLE] || !c->disable_stall) : !c->stretch)) {
        c->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + c->remaining;
        timer_mod(c->timer, c->deadline);
    }
}
static void ws53_spi_schedule(WS53SpiChannel *c, bool enable_event, int64_t ns)
{
    timer_del(c->timer); c->scheduled = true; c->enable_event = enable_event;
    c->remaining = ns; ws53_spi_resume(c);
}
static unsigned ws53_spi_bits(WS53SpiChannel *c)
{
    return ((c->regs[SP_CONTROL] >> (c->index ? 16 : 13)) & 31) + 1;
}
static void ws53_spi_next(WS53SpiChannel *c)
{
    if (!c->enabled || !c->tx_len || c->scheduled || !c->regs[SP_SELECT]) { return; }
    uint64_t clocks = (uint64_t)c->regs[SP_BAUD] * ws53_spi_bits(c);
    ws53_spi_schedule(c, false, (clocks * 1000 + 31) / 32);
}
static void ws53_spi_tick(void *opaque)
{
    WS53SpiChannel *c = opaque; c->scheduled = false;
    if (c->enable_event) {
        c->enabled = c->regs[SP_ENABLE];
        if (!c->enabled) { c->tx_len = c->rx_len = 0; }
    } else {
        uint32_t word = c->tx[c->tx_head];
        c->tx_head = (c->tx_head + 1) % 8; c->tx_len--; c->frames++;
        if (c->collision) {
            c->latched |= SP_MULTI;
        } else if (c->rx_len == 8) {
            c->latched |= SP_RX_OVER;
        } else {
            uint32_t mask = ws53_spi_bits(c) == 8 ? 0xff : UINT32_MAX;
            /* Stateless full-duplex virtual peer; every MOSI word determines MISO. */
            c->rx[(c->rx_head + c->rx_len) % 8] = (word ^ 0xa5a5a5a5u) & mask;
            c->rx_len++;
        }
    }
    ws53_spi_next(c); ws53_spi_irq(c);
}
static int ws53_spi_reg(WS53SpiChannel *c, hwaddr off)
{
    for (unsigned i = 0; i < SP_REG_COUNT; i++) {
        if (ws53_spi_offsets[c->index][i] == off) { return i; }
    }
    return -1;
}
static bool ws53_spi_fifo(WS53SpiChannel *c, hwaddr off)
{
    unsigned start = c->index ? 0x60 : 0x2c;
    return off >= start && off < start + 36 * 4;
}
static uint64_t ws53_spi_read(void *opaque, hwaddr off, unsigned size)
{
    WS53SpiChannel *c = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (ws53_spi_fifo(c, off)) {
        if (!c->rx_len) { c->latched |= SP_RX_UNDER; ws53_spi_irq(c); return 0; }
        uint32_t v = c->rx[c->rx_head]; c->rx_head = (c->rx_head + 1) % 8; c->rx_len--;
        ws53_spi_irq(c); return v;
    }
    int r = ws53_spi_reg(c, off);
    uint32_t clear = 0;
    switch (r) {
    case SP_RAW: return ws53_spi_encode(c, ws53_spi_raw(c));
    case SP_MASKED: return ws53_spi_encode(c, ws53_spi_raw(c)) & c->regs[SP_MASK];
    case SP_TX_LEVEL: return c->tx_len;
    case SP_RX_LEVEL: return c->rx_len;
    case SP_STATUS: {
        bool busy = c->tx_len || (c->enable_event && c->scheduled && c->enabled);
        if (c->index) {
            return busy | (c->tx_len < 8 ? 2 : 0) | (!c->tx_len ? 4 : 0) |
                (c->rx_len ? 8 : 0) | (c->rx_len == 8 ? 16 : 0) |
                (c->latched & SP_TX_OVER ? 32 : 0) | (c->latched & SP_MULTI ? 64 : 0);
        }
        return (busy ? 0x8000 : 0) | (c->tx_len < 8 ? 0x800 : 0) |
            (!c->tx_len ? 0x1000 : 0) | (c->rx_len ? 16 : 0) |
            (c->rx_len == 8 ? 32 : 0) | (!!(c->latched & (SP_MULTI | SP_TX_OVER)));
    }
    case SP_CLEAR_TX: clear = SP_TX_OVER; break;
    case SP_CLEAR_OVER: clear = SP_RX_OVER; break;
    case SP_CLEAR_UNDER: clear = SP_RX_UNDER; break;
    case SP_CLEAR_MULTI: clear = SP_MULTI; break;
    case SP_CLEAR_ALL: clear = 0x2e; break;
    case SP_VERSION: case SP_ID: case -1: goto invalid;
    default: return c->regs[r];
    }
    uint32_t v = !!(c->latched & clear); c->latched &= ~clear; ws53_spi_irq(c); return v;
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-spi%u: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", c->index, off, size);
    return 0;
}
static void ws53_spi_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53SpiChannel *c = opaque; WS53SpiState *s = c->owner;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (ws53_spi_fifo(c, off)) {
        if (!c->regs[SP_ENABLE] || !c->regs[SP_SELECT]) { goto invalid; }
        if (c->tx_len == 8) { c->latched |= SP_TX_OVER; }
        else { c->tx[(c->tx_head + c->tx_len) % 8] = v; c->tx_len++; }
        ws53_spi_next(c); ws53_spi_irq(c); return;
    }
    int r = ws53_spi_reg(c, off);
    switch (r) {
    case SP_ENABLE:
        if (v > 1) { goto invalid; }
        if (v && !s->assume_profile) {
            qemu_log_mask(LOG_GUEST_ERROR, "ws53-spi: unimplemented transfer without assume-profile\n"); return;
        }
        if (v && ((ws53_spi_bits(c) != 8 && ws53_spi_bits(c) != 32) ||
            c->regs[SP_BAUD] < 2 || (!c->index && !(s->gate[1].value & 1)))) { goto invalid; }
        if (c->regs[r] == v && (c->enabled == !!v || c->enable_event)) { return; }
        c->regs[r] = v;
        ws53_spi_schedule(c, true, 1000); ws53_spi_irq(c); return;
    case SP_MASK:
        if (v & ~ws53_spi_encode(c, 0x3f)) { goto invalid; } break;
    case SP_TX_THRESHOLD: case SP_RX_THRESHOLD:
        if (v > 7) { goto invalid; } break;
    case SP_CLEAR_TX: case SP_CLEAR_OVER: case SP_CLEAR_UNDER: case SP_CLEAR_MULTI: case SP_CLEAR_ALL:
        return; /* Source says writes have no effect. No accidental RMW acknowledgement. */
    case SP_SELECT:
        if (v > 1 || (c->tx_len && v != c->regs[r])) { goto invalid; }
        c->regs[r] = v; ws53_spi_next(c); return;
    case SP_DMA: case SP_DMA_RX: case SP_DMA_TX:
        if (v) { goto invalid; } break;
    case SP_CONTROL: {
        unsigned shift = c->index ? 16 : 13;
        uint32_t allowed = (31u << shift) | (c->index ? 0xc0 : 0x18);
        if (c->enabled || c->regs[SP_ENABLE] || (v & ~allowed)) { goto invalid; }
        unsigned dfs = (v >> shift) & 31;
        if (dfs != 7 && dfs != 31) { goto invalid; } break;
    }
    case SP_BAUD:
        if (c->enabled || c->regs[SP_ENABLE] || v > 65534 || (v & 1)) { goto invalid; } break;
    case SP_COUNT: case SP_ENHANCED: case SP_MICROWIRE:
        if (c->enabled || c->regs[SP_ENABLE] || v) { goto invalid; } break;
    case SP_SAMPLE: case SP_EDGE:
        if (c->enabled || c->regs[SP_ENABLE] || v > 255) { goto invalid; } break;
    default: goto invalid;
    }
    c->regs[r] = v; ws53_spi_irq(c); return;
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-spi%u: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", c->index, off, v, size);
}
static uint64_t ws53_spi_gate_read(void *opaque, hwaddr off, unsigned size)
{
    WS53SpiGate *g = opaque;
    if (!off && size == 2) { return g->value; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-spi: unimplemented gate%u read size=%u\n", g->index, size); return 0;
}
static void ws53_spi_gate_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53SpiGate *g = opaque; WS53SpiState *s = g->owner;
    const uint16_t masks[] = {1,1,0x80,0x1ff,0x2a};
    if (off || size != 2 || ((v ^ g->value) & ~masks[g->index]) ||
        (g->index == 3 && (v & 0x1ff) > 1) ||
        (g->index == 1 && v != g->value &&
         (s->channel[0].enabled || s->channel[0].regs[SP_ENABLE]))) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-spi: unimplemented gate%u write value=0x%" PRIx64 " size=%u\n", g->index, v, size); return;
    }
    for (unsigned i = 0; i < 2; i++) { ws53_spi_pause(&s->channel[i]); }
    g->value = v;
    for (unsigned i = 0; i < 2; i++) { ws53_spi_resume(&s->channel[i]); }
}
#define WS53_SPI_OPS(name, reader, writer) \
    static const MemoryRegionOps name = { .read=reader, .write=writer, .endianness=DEVICE_LITTLE_ENDIAN, \
        .valid={.min_access_size=1,.max_access_size=8,.unaligned=true}, \
        .impl={.min_access_size=1,.max_access_size=8,.unaligned=true} }
WS53_SPI_OPS(ws53_spi_ops, ws53_spi_read, ws53_spi_write);
WS53_SPI_OPS(ws53_spi_gate_ops, ws53_spi_gate_read, ws53_spi_gate_write);
#undef WS53_SPI_OPS
static void ws53_spi_mclken1(void *opaque, int n, int level)
{
    WS53SpiState *s = opaque; ws53_spi_pause(&s->channel[0]);
    s->spi_gate = level; ws53_spi_resume(&s->channel[0]);
}
static void ws53_spi_global_gate(void *opaque, int n, int level)
{
    WS53SpiState *s = opaque; ws53_spi_pause(&s->channel[1]);
    s->gate[2].value = level ? 0x80 : 0; ws53_spi_resume(&s->channel[1]);
}
#define WS53_SPI_INPUT(name) \
    static void ws53_spi_##name(void *opaque, int n, int level) { \
        WS53SpiChannel *c = &WS53_SPI(opaque)->channel[n]; \
        ws53_spi_pause(c); c->name = level; ws53_spi_resume(c); }
WS53_SPI_INPUT(clock)
WS53_SPI_INPUT(stretch)
WS53_SPI_INPUT(collision)
WS53_SPI_INPUT(disable_stall)
#undef WS53_SPI_INPUT
static void ws53_spi_reset(void *opaque)
{
    WS53SpiState *s = opaque;
    for (unsigned i = 0; i < 5; i++) { if (i != 2) { s->gate[i].value = 0; } }
    for (unsigned i = 0; i < 2; i++) {
        WS53SpiChannel *c = &s->channel[i]; timer_del(c->timer);
        memset(c->regs, 0, sizeof(c->regs)); c->latched = 0;
        c->tx_head = c->tx_len = c->rx_head = c->rx_len = 0;
        c->enabled = c->scheduled = c->enable_event = false; c->frames = 0;
        ws53_spi_irq(c);
    }
}
static void ws53_spi_realize(DeviceState *dev, Error **errp)
{
    WS53SpiState *s = WS53_SPI(dev);
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-spi: assumed profile 32MHz FIFO8 XOR-peer enable-crossing=1us\n");
    }
    qemu_register_reset(ws53_spi_reset, s);
}
static const Property ws53_spi_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53SpiState, assume_profile, false),
};
static void ws53_spi_init(Object *obj)
{
    WS53SpiState *s = WS53_SPI(obj);
    for (unsigned i = 0; i < 2; i++) {
        WS53SpiChannel *c = &s->channel[i]; c->owner = s; c->index = i; c->clock = true;
        memory_region_init_io(&c->mmio, obj, &ws53_spi_ops, c, TYPE_WS53_SPI, 0x1000);
        sysbus_init_mmio(SYS_BUS_DEVICE(obj), &c->mmio); sysbus_init_irq(SYS_BUS_DEVICE(obj), &c->irq);
        c->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_spi_tick, c);
        g_autofree char *name = g_strdup_printf("frames%u", i);
        object_property_add_uint64_ptr(obj, name, &c->frames, OBJ_PROP_FLAG_READ);
    }
    for (unsigned i = 0; i < 5; i++) {
        if (i == 2) { continue; } /* Shared GLOBAL_CLKEN is an external provider. */
        WS53SpiGate *g = &s->gate[i]; g->owner = s; g->index = i;
        memory_region_init_io(&g->mmio, obj, &ws53_spi_gate_ops, g, "ws53-spi-gate", 2);
        sysbus_init_mmio(SYS_BUS_DEVICE(obj), &g->mmio);
    }
    qdev_init_gpio_in_named(DEVICE(obj), ws53_spi_mclken1, "gate", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_spi_global_gate, "global-gate", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_spi_clock, "clock", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_spi_stretch, "stretch", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_spi_collision, "collision", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_spi_disable_stall, "disable-stall", 2);
}
static void ws53_spi_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_spi_realize;
    device_class_set_props(dc, ws53_spi_properties);
}
static const TypeInfo ws53_spi_type = {
    .name=TYPE_WS53_SPI,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53SpiState),
    .instance_init=ws53_spi_init,.class_init=ws53_spi_class_init,
};
