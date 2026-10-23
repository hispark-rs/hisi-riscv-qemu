/* WS53 non-PRO I2C v151, fbb_ws53 163d9150 / hisi-registers 01dcce5.
 * Explicit functional profile, not measured silicon timing/reset behavior.
 * Built-in volatile 256-byte register peers at 7-bit 0x50 / 10-bit 0x250.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define TYPE_WS53_I2C "ws53-i2c"
OBJECT_DECLARE_SIMPLE_TYPE(WS53I2cState, WS53_I2C)

typedef struct WS53I2cChannel {
    WS53I2cState *owner;
    unsigned index;
    MemoryRegion mmio;
    QEMUTimer *timer;
    qemu_irq irq;
    uint32_t regs[0xc8 / 4], latched, abort_source, abort_detail, flushed;
    uint16_t tx[8], rx[8];
    unsigned tx_head, tx_len, rx_head, rx_len;
    bool enabled, active, direction, first_write, pending_read_ack;
    bool clock, stretch, nack_address, nack_data, arbitration;
    bool scheduled, enable_event;
    int64_t remaining, deadline;
    uint8_t memory[256], pointer;
    uint64_t starts, restarts, stops, read_acks, read_nacks;
} WS53I2cChannel;
struct WS53I2cState {
    SysBusDevice parent_obj;
    MemoryRegion gate_mmio;
    WS53I2cChannel channel[2];
    uint16_t gate;
    bool assume_profile, warned;
};
static uint32_t ws53_i2c_raw(WS53I2cChannel *c)
{
    return c->latched | (c->rx_len > c->regs[0x50 / 4] ? 4 : 0) |
        (c->enabled && c->tx_len <= c->regs[0x54 / 4] ? 16 : 0);
}
static void ws53_i2c_irq(WS53I2cChannel *c)
{
    qemu_set_irq(c->irq, ws53_i2c_raw(c) & c->regs[0xb0 / 4]);
}
static bool ws53_i2c_clocked(WS53I2cChannel *c)
{
    return c->owner->assume_profile && c->clock &&
        (c->owner->gate & (0x40u << c->index));
}
static void ws53_i2c_pause(WS53I2cChannel *c)
{
    if (c->scheduled && timer_pending(c->timer)) {
        c->remaining = MAX(c->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(c->timer);
    }
}
static void ws53_i2c_resume(WS53I2cChannel *c)
{
    if (c->scheduled && !timer_pending(c->timer) && ws53_i2c_clocked(c) &&
        (c->enable_event || !c->stretch)) {
        c->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + c->remaining;
        timer_mod(c->timer, c->deadline);
    }
}
static void ws53_i2c_schedule(WS53I2cChannel *c, bool enable_event, int64_t delay)
{
    timer_del(c->timer);
    c->scheduled = true; c->enable_event = enable_event; c->remaining = delay;
    ws53_i2c_resume(c);
}
static void ws53_i2c_abort(WS53I2cChannel *c, uint32_t source, uint32_t detail)
{
    c->abort_source |= source; c->abort_detail |= detail; c->latched |= 64;
    c->flushed = c->tx_len;
    c->tx_len = c->rx_len = 0; c->active = c->pending_read_ack = false;
    c->scheduled = false; timer_del(c->timer); ws53_i2c_irq(c);
}
static void ws53_i2c_start_next(WS53I2cChannel *c)
{
    if (!c->enabled || !c->tx_len || c->scheduled || (c->latched & 64)) { return; }
    uint16_t cmd = c->tx[c->tx_head];
    if ((cmd & 0x100) && c->rx_len == 8 && (c->regs[0] & 0x200)) { return; }
    unsigned mode = (c->regs[0] >> 1) & 3;
    unsigned off = mode == 1 ? 0x30 : mode == 2 ? 0x3c : 0x44;
    uint64_t period = c->regs[off / 4] + 8 + c->regs[(off + 4) / 4] + 1;
    /* One byte including ACK; initial/repeated address adds 9 or 18 clocks.
     * No analog edges, bus rise time or silicon SDA hold proof is implied. */
    unsigned clocks = 9;
    if (!c->active || (cmd & 0x400)) { clocks += (c->regs[0] & 16) ? 18 : 9; }
    ws53_i2c_schedule(c, false, (period * clocks * 1000 + 31) / 32);
}
static void ws53_i2c_tick(void *opaque)
{
    WS53I2cChannel *c = opaque;
    c->scheduled = false;
    if (c->enable_event) {
        c->enabled = c->regs[1] & 1;
        if (!c->enabled) {
            if (c->pending_read_ack) { c->read_nacks++; }
            c->tx_len = c->rx_len = 0;
            c->active = c->pending_read_ack = false;
        }
        ws53_i2c_start_next(c); ws53_i2c_irq(c); return;
    }
    uint16_t cmd = c->tx[c->tx_head];
    bool read = cmd & 0x100;
    if (c->arbitration) { ws53_i2c_abort(c, 0, 1 << 12); return; }
    if (!c->active || (cmd & 0x400)) {
        bool ten = c->regs[0] & 16;
        if (c->nack_address || (c->regs[0x10 / 4] & 0x3ff) != (ten ? 0x250 : 0x50)) {
            ws53_i2c_abort(c, 0, ten ? 2 : 1); return;
        }
        if (c->active && !(c->regs[0] & 32)) {
            ws53_i2c_abort(c, 0, 1 << 10); return;
        }
        if (c->active) { c->restarts++; } else { c->starts++; }
        c->latched |= c->active ? 0x1000 : 0x400;
        c->active = true; c->direction = read; c->first_write = !read;
    } else if (read != c->direction) {
        /* The profile requires explicit RESTART for a direction change. */
        ws53_i2c_abort(c, 0, 1 << 10); return;
    }
    if (c->pending_read_ack) {
        if (read && !(cmd & 0x400)) { c->read_acks++; }
        else { c->read_nacks++; }
    }
    c->tx_head = (c->tx_head + 1) % 8; c->tx_len--;
    if (read) {
        if (c->rx_len == 8) {
            c->latched |= 2;
        } else {
            c->rx[(c->rx_head + c->rx_len) % 8] = c->memory[c->pointer++];
            c->rx_len++;
        }
        /* ACK is deferred across an empty TX FIFO; the next command decides
         * continuation versus last-read NACK. This is an explicit assumption. */
        c->pending_read_ack = !(cmd & 0x200);
        if (cmd & 0x200) { c->read_nacks++; }
    } else {
        c->pending_read_ack = false;
        if (c->nack_data) { ws53_i2c_abort(c, 0, 8); return; }
        if (c->first_write) { c->pointer = cmd; c->first_write = false; }
        else { c->memory[c->pointer++] = cmd; }
    }
    if (cmd & 0x200) {
        c->active = c->pending_read_ack = false; c->latched |= 0x200; c->stops++;
    }
    ws53_i2c_start_next(c); ws53_i2c_irq(c);
}
static uint64_t ws53_i2c_read(void *opaque, hwaddr off, unsigned size)
{
    WS53I2cChannel *c = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 0: case 4: case 0x10: case 0x30: case 0x34: case 0x3c: case 0x40:
    case 0x44: case 0x48: case 0x50: case 0x54: case 0x6c: case 0x70:
    case 0xa0: case 0xac: case 0xb0: return c->regs[off / 4];
    case 0x1c: {
        if (!c->rx_len) { c->latched |= 1; ws53_i2c_irq(c); return 0; }
        uint16_t v = c->rx[c->rx_head];
        c->rx_head = (c->rx_head + 1) % 8; c->rx_len--;
        ws53_i2c_start_next(c); ws53_i2c_irq(c); return v;
    }
    case 0x60: return (c->active ? 0x21 : 0) | (c->tx_len < 8 ? 2 : 0) |
        (!c->tx_len ? 4 : 0) | (c->rx_len ? 8 : 0) | (c->rx_len == 8 ? 16 : 0) |
        (c->active && !c->tx_len ? 128 : 0) |
        (c->rx_len == 8 && c->tx_len && (c->regs[0] & 0x200) ? 256 : 0);
    case 0x64: return c->tx_len;
    case 0x68: return c->rx_len;
    case 0x74: return c->flushed;
    case 0x78: return c->abort_source;
    case 0x7c: return c->abort_detail;
    case 0x84: return c->enabled;
    case 0xb4: return ws53_i2c_raw(c) & c->regs[0xb0 / 4];
    case 0xb8: return ws53_i2c_raw(c);
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2c%u: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", c->index, off, size);
    return 0;
}
static void ws53_i2c_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53I2cChannel *c = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 4:
        if (v & ~3u) { goto invalid; }
        if ((v & 1) && c->owner->assume_profile) {
            unsigned mode = (c->regs[0] >> 1) & 3;
            unsigned counts = mode == 1 ? 0x30 : mode == 2 ? 0x3c : 0x44;
            unsigned min_low = mode == 1 ? 12 : mode == 2 ? 15 : 21;
            if ((c->regs[0] & 0x41) != 0x41 || !mode ||
                c->regs[counts / 4] < 6 || c->regs[(counts + 4) / 4] < min_low) {
                goto invalid;
            }
        }
        c->regs[1] = v & 1;
        if (v & 2) { ws53_i2c_abort(c, 1, 0); }
        if ((v & 1) && !c->owner->warned) {
            qemu_log_mask(LOG_GUEST_ERROR, c->owner->assume_profile ?
                "ws53-i2c: assumed profile 32MHz FIFO8 CON-addressing deferred-read-ACK disable=1us\n" :
                "ws53-i2c: unimplemented transfer without assume-profile\n");
            c->owner->warned = true;
        }
        ws53_i2c_schedule(c, true, 1000); return;
    case 0x1c:
        if (v > 0x7ff) { goto invalid; }
        if (!c->enabled) { ws53_i2c_abort(c, 0, 1 << 11); return; }
        if (c->latched & 64) { return; } /* abort latches FIFO until clear */
        if (c->tx_len == 8) { c->latched |= 8; ws53_i2c_irq(c); return; }
        c->tx[(c->tx_head + c->tx_len) % 8] = v; c->tx_len++;
        ws53_i2c_start_next(c); ws53_i2c_irq(c); return;
    case 0xc4:
        if (v != 1) { goto invalid; }
        c->latched = c->abort_source = c->abort_detail = c->flushed = 0;
        ws53_i2c_start_next(c); ws53_i2c_irq(c); return;
    case 0xb0:
        if (v & ~0x165fu) { goto invalid; }
        c->regs[off / 4] = v; ws53_i2c_irq(c); return;
    case 0xa0: if (v) { goto invalid; } c->regs[off / 4] = 0; return;
    case 0:
        if (c->enabled || (v & ~0x277u) || !(v & 1) || !(v & 64) || !(v & 6)) { goto invalid; }
        break;
    case 0x10:
        if (c->enabled || (v & ~0x3ffu)) { goto invalid; }
        break;
    case 0x30: case 0x34: case 0x3c: case 0x40: case 0x44: case 0x48:
    case 0x70:
        if (c->enabled || v > 0xffff) { goto invalid; } break;
    case 0x50: case 0x54:
        if (v > 7) { goto invalid; } break;
    case 0x6c: case 0xac:
        if (c->enabled || v > 255) { goto invalid; } break;
    default: goto invalid;
    }
    c->regs[off / 4] = v; ws53_i2c_irq(c); return;
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2c%u: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", c->index, off, v, size);
}
static uint64_t ws53_i2c_gate_read(void *opaque, hwaddr off, unsigned size)
{
    WS53I2cState *s = opaque;
    if (!off && size == 2) { return s->gate; }
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2c: unimplemented gate read size=%u\n", size); return 0;
}
static void ws53_i2c_gate_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53I2cState *s = opaque;
    /* Preserve inherited TIMER/UART bits; reject changes to unmodeled clocks. */
    if (off || size != 2 || ((v ^ s->gate) & ~0xc0u)) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-i2c: unimplemented gate write value=0x%" PRIx64 " size=%u\n", v, size); return;
    }
    for (unsigned i = 0; i < 2; i++) { ws53_i2c_pause(&s->channel[i]); }
    s->gate = v;
    for (unsigned i = 0; i < 2; i++) { ws53_i2c_resume(&s->channel[i]); }
}
#define WS53_I2C_OPS(name, reader, writer) \
    static const MemoryRegionOps name = { .read=reader, .write=writer, \
        .endianness=DEVICE_LITTLE_ENDIAN, \
        .valid={.min_access_size=1,.max_access_size=8,.unaligned=true}, \
        .impl={.min_access_size=1,.max_access_size=8,.unaligned=true} }
WS53_I2C_OPS(ws53_i2c_ops, ws53_i2c_read, ws53_i2c_write);
WS53_I2C_OPS(ws53_i2c_gate_ops, ws53_i2c_gate_read, ws53_i2c_gate_write);
#undef WS53_I2C_OPS
/* Fault inputs are external to the controller and survive system reset. */
static void ws53_i2c_clock(void *opaque, int n, int level)
{
    WS53I2cState *s = opaque; WS53I2cChannel *c = &s->channel[n];
    ws53_i2c_pause(c); c->clock = level; ws53_i2c_resume(c);
}
static void ws53_i2c_stretch(void *opaque, int n, int level)
{
    WS53I2cState *s = opaque; WS53I2cChannel *c = &s->channel[n];
    ws53_i2c_pause(c); c->stretch = level; ws53_i2c_resume(c);
}
static void ws53_i2c_nack_address(void *opaque, int n, int level)
{
    WS53I2cState *s = opaque; s->channel[n].nack_address = level;
}
static void ws53_i2c_nack_data(void *opaque, int n, int level)
{
    WS53I2cState *s = opaque; s->channel[n].nack_data = level;
}
static void ws53_i2c_arbitration(void *opaque, int n, int level)
{
    WS53I2cState *s = opaque; s->channel[n].arbitration = level;
}
static void ws53_i2c_reset(void *opaque)
{
    WS53I2cState *s = opaque;
    s->gate = 0x11; /* inherited boot-profile timer/UART; not a silicon reset */
    for (unsigned i = 0; i < 2; i++) {
        WS53I2cChannel *c = &s->channel[i];
        timer_del(c->timer); memset(c->regs, 0, sizeof(c->regs));
        c->tx_head = c->tx_len = c->rx_head = c->rx_len = 0;
        c->latched = c->abort_source = c->abort_detail = c->flushed = 0;
        c->active = c->enabled = c->scheduled = c->pending_read_ack = false;
        c->pointer = 0;
        c->starts = c->restarts = c->stops = c->read_acks = c->read_nacks = 0;
        for (unsigned j = 0; j < 256; j++) { c->memory[j] = j ^ 0xa5; }
        ws53_i2c_irq(c);
    }
}
static void ws53_i2c_realize(DeviceState *dev, Error **errp)
{
    qemu_register_reset(ws53_i2c_reset, WS53_I2C(dev));
}
static const Property ws53_i2c_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53I2cState, assume_profile, false),
};
static void ws53_i2c_init(Object *obj)
{
    WS53I2cState *s = WS53_I2C(obj);
    for (unsigned i = 0; i < 2; i++) {
        WS53I2cChannel *c = &s->channel[i]; c->owner = s; c->index = i; c->clock = true;
        memory_region_init_io(&c->mmio, obj, &ws53_i2c_ops, c, TYPE_WS53_I2C, 0x1000);
        sysbus_init_mmio(SYS_BUS_DEVICE(obj), &c->mmio);
        sysbus_init_irq(SYS_BUS_DEVICE(obj), &c->irq);
        c->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_i2c_tick, c);
        g_autofree char *starts = g_strdup_printf("starts%u", i);
        g_autofree char *restarts = g_strdup_printf("restarts%u", i);
        g_autofree char *stops = g_strdup_printf("stops%u", i);
        g_autofree char *acks = g_strdup_printf("read-acks%u", i);
        g_autofree char *nacks = g_strdup_printf("read-nacks%u", i);
        object_property_add_uint64_ptr(obj, starts, &c->starts, OBJ_PROP_FLAG_READ);
        object_property_add_uint64_ptr(obj, restarts, &c->restarts, OBJ_PROP_FLAG_READ);
        object_property_add_uint64_ptr(obj, stops, &c->stops, OBJ_PROP_FLAG_READ);
        object_property_add_uint64_ptr(obj, acks, &c->read_acks, OBJ_PROP_FLAG_READ);
        object_property_add_uint64_ptr(obj, nacks, &c->read_nacks, OBJ_PROP_FLAG_READ);
    }
    memory_region_init_io(&s->gate_mmio, obj, &ws53_i2c_gate_ops, s, "ws53-i2c-gate", 2);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->gate_mmio);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2c_clock, "clock", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2c_stretch, "stretch", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2c_nack_address, "nack-address", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2c_nack_data, "nack-data", 2);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_i2c_arbitration, "arbitration", 2);
}
static void ws53_i2c_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_i2c_realize; device_class_set_props(dc, ws53_i2c_properties);
}
static const TypeInfo ws53_i2c_type = {
    .name=TYPE_WS53_I2C,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53I2cState),
    .instance_init=ws53_i2c_init,.class_init=ws53_i2c_class_init,
};
