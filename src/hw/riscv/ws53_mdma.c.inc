/* WS53 ROM v151 MDMA: SDK163d9150, source contract f13eaaf.
 * Explicit digital mem-to-mem profile only, not PL080/WS63 layout reuse.
 * Posted writes survive halt/disable; clock stalls preserve remaining time.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_MDMA "ws53-mdma"
OBJECT_DECLARE_SIMPLE_TYPE(WS53MdmaState, WS53_MDMA)
typedef struct WS53MdmaChannel {
    WS53MdmaState *owner;
    QEMUTimer *read_timer, *write_timer;
    uint32_t source, destination, control, configuration;
    uint8_t index, data[4], width;
    bool in_flight, stalled, read_error, write_error, bus_stall;
    int64_t read_remaining, write_remaining, read_deadline, write_deadline;
} WS53MdmaChannel;
struct WS53MdmaState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    WS53MdmaChannel channel[8];
    uint32_t configuration, raw_tc, raw_error;
    bool assume_profile, clock_on;
    uint64_t phase_ns, reads, writes, errors, transfers, pending_writes;
};
static void ws53_mdma_irq(WS53MdmaState *s)
{
    unsigned pending = 0;
    for (unsigned i = 0; i < 8; i++) {
        uint32_t cfg = s->channel[i].configuration;
        if (((s->raw_tc & (1u << i)) && (cfg & 0x2000)) ||
            ((s->raw_error & (1u << i)) && (cfg & 0x1000))) { pending |= 1u << i; }
    }
    qemu_set_irq(s->irq, !!pending);
}
static void ws53_mdma_pause(WS53MdmaChannel *c)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (timer_pending(c->read_timer)) {
        c->read_remaining = MAX(c->read_deadline - now, 1); timer_del(c->read_timer);
    }
    if (timer_pending(c->write_timer)) {
        c->write_remaining = MAX(c->write_deadline - now, 1); timer_del(c->write_timer);
    }
}
static void ws53_mdma_resume(WS53MdmaChannel *c)
{
    WS53MdmaState *s = c->owner;
    if (!s->clock_on || c->stalled) { return; }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (c->in_flight) {
        if (!c->bus_stall && !timer_pending(c->write_timer)) {
            c->write_deadline = now + c->write_remaining; timer_mod(c->write_timer, c->write_deadline);
        }
    } else if ((s->configuration & 1) && (c->configuration & 1) && !(c->configuration & 0x10000) &&
               (c->control & 0xfff) && !timer_pending(c->read_timer)) {
        c->read_deadline = now + c->read_remaining; timer_mod(c->read_timer, c->read_deadline);
    }
}
static void ws53_mdma_error(WS53MdmaChannel *c)
{
    WS53MdmaState *s = c->owner;
    c->configuration &= ~1u; s->raw_error |= 1u << c->index; s->errors++;
    ws53_mdma_irq(s);
}
static void ws53_mdma_read_beat(void *opaque)
{
    WS53MdmaChannel *c = opaque; WS53MdmaState *s = c->owner;
    MemTxResult result = c->read_error ? MEMTX_ERROR :
        address_space_read(&address_space_memory, c->source, MEMTXATTRS_UNSPECIFIED, c->data, c->width);
    if (result != MEMTX_OK) { ws53_mdma_error(c); return; }
    s->reads++; s->pending_writes++; c->in_flight = true;
    c->write_remaining = s->phase_ns; ws53_mdma_resume(c);
}
static void ws53_mdma_write_beat(void *opaque)
{
    WS53MdmaChannel *c = opaque; WS53MdmaState *s = c->owner;
    MemTxResult result = c->write_error ? MEMTX_ERROR :
        address_space_write(&address_space_memory, c->destination, MEMTXATTRS_UNSPECIFIED, c->data, c->width);
    c->in_flight = false; s->pending_writes--;
    if (result != MEMTX_OK) { ws53_mdma_error(c); return; }
    s->writes++;
    if (c->control & (1u << 26)) { c->source += c->width; }
    if (c->control & (1u << 27)) { c->destination += c->width; }
    unsigned left = (c->control & 0xfff) - 1;
    c->control = (c->control & ~0xfffu) | left;
    if (!left) {
        c->configuration &= ~1u; s->transfers++;
        if (c->control & (1u << 31)) { s->raw_tc |= 1u << c->index; }
        ws53_mdma_irq(s);
    }
    c->read_remaining = s->phase_ns; ws53_mdma_resume(c);
}
static bool ws53_mdma_range(uint32_t address, unsigned count, unsigned width, bool increment)
{
    uint64_t limit = (uint64_t)address + (increment ? count : 1) * width;
    return !(address & (width - 1)) && address >= 0x20010000 && limit <= 0x20060000;
}
static bool ws53_mdma_valid(WS53MdmaChannel *c)
{
    unsigned count = c->control & 0xfff, width = 1u << ((c->control >> 18) & 7);
    return count && ws53_mdma_range(c->source, count, width, c->control & (1u << 26)) &&
           ws53_mdma_range(c->destination, count, width, c->control & (1u << 27));
}
static uint64_t ws53_mdma_read(void *opaque, hwaddr off, unsigned size)
{
    WS53MdmaState *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    switch (off) {
    case 4: {
        uint32_t tc = 0, err = 0;
        for (unsigned i = 0; i < 8; i++) {
            if (s->channel[i].configuration & 0x2000) { tc |= s->raw_tc & (1u << i); }
            if (s->channel[i].configuration & 0x1000) { err |= s->raw_error & (1u << i); }
        }
        return (tc | err) | (tc << 8) | (err << 16);
    }
    case 0xc: return s->raw_tc | (s->raw_error << 8);
    case 0x10: {
        uint32_t enabled = 0;
        for (unsigned i = 0; i < 8; i++) { enabled |= (s->channel[i].configuration & 1) << i; }
        return enabled;
    }
    case 0x1c: return s->configuration;
    default: break;
    }
    if (off >= 0x100 && off < 0x200) {
        WS53MdmaChannel *c = &s->channel[(off - 0x100) / 0x20];
        switch (off & 0x1f) {
        case 0: return 0; /* Only unlinked transfer supported. */
        case 4: return c->destination;
        case 8: return c->configuration | (c->in_flight ? 0x8000 : 0);
        case 0x10: return c->source;
        case 0x14: return c->control;
        default: break;
        }
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-mdma: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size); return 0;
}
static void ws53_mdma_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53MdmaState *s = opaque; uint32_t v = value;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (off == 8) {
        if (v & ~0xffffu) { goto invalid; }
        s->raw_tc &= ~(v & 255); s->raw_error &= ~((v >> 8) & 255); ws53_mdma_irq(s); return;
    }
    if (off == 0x1c) {
        if (v & ~1u) { goto invalid; }
        for (unsigned i = 0; i < 8; i++) { ws53_mdma_pause(&s->channel[i]); }
        s->configuration = v;
        for (unsigned i = 0; i < 8; i++) { ws53_mdma_resume(&s->channel[i]); }
        return;
    }
    if (off >= 0x100 && off < 0x200) {
        WS53MdmaChannel *c = &s->channel[(off - 0x100) / 0x20];
        unsigned reg = off & 0x1f;
        if (reg == 8) {
            /* ACTIVE is status-only: ignore its value during a PAC field RMW.
             * This profile supports mem-to-mem, no request/lock/flow extensions. */
            if (v & ~0x1b001u) { goto invalid; }
            v &= ~0x8000u;
            if (!(c->configuration & 1) && (v & 1)) {
                if (c->in_flight || !ws53_mdma_valid(c)) { goto invalid; }
                if (!s->assume_profile) {
                    qemu_log_mask(LOG_GUEST_ERROR, "ws53-mdma: unimplemented transfer without assume-profile\n"); return;
                }
                c->width = 1u << ((c->control >> 18) & 7); c->read_remaining = s->phase_ns;
            }
            ws53_mdma_pause(c); c->configuration = v; ws53_mdma_resume(c); ws53_mdma_irq(s); return;
        }
        if ((c->configuration & 1) || c->in_flight) { goto invalid; }
        switch (reg) {
        case 0: if (v) { goto invalid; } return;
        case 4: c->destination = v; return;
        case 0x10: c->source = v; return;
        case 0x14: {
            unsigned sw = (v >> 18) & 7, dw = (v >> 21) & 7;
            /* One-beat bursts, same-width 8/16/32-bit elements, master0/prot0. */
            if ((v & ~0x8cfc0fffu) || sw > 2 || dw != sw) { goto invalid; }
            c->control = v; return;
        }
        default: break;
        }
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-mdma: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, value, size);
}
static const MemoryRegionOps ws53_mdma_ops = {
    .read=ws53_mdma_read,.write=ws53_mdma_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_mdma_clock(void *opaque, int n, int level)
{
    WS53MdmaState *s = opaque;
    for (unsigned i = 0; i < 8; i++) { ws53_mdma_pause(&s->channel[i]); }
    s->clock_on = !!level;
    for (unsigned i = 0; i < 8; i++) { ws53_mdma_resume(&s->channel[i]); }
}
#define WS53_MDMA_INPUT(name, field) \
static void ws53_mdma_##name(void *opaque, int n, int level) { \
    WS53MdmaState *s = opaque; WS53MdmaChannel *c = &s->channel[n]; \
    ws53_mdma_pause(c); c->field = !!level; ws53_mdma_resume(c); }
WS53_MDMA_INPUT(stall, stalled)
WS53_MDMA_INPUT(bus_stall, bus_stall)
WS53_MDMA_INPUT(read_error, read_error)
WS53_MDMA_INPUT(write_error, write_error)
#undef WS53_MDMA_INPUT
static void ws53_mdma_reset(void *opaque)
{
    WS53MdmaState *s = opaque;
    for (unsigned i = 0; i < 8; i++) {
        WS53MdmaChannel *c = &s->channel[i]; timer_del(c->read_timer); timer_del(c->write_timer);
        c->source = c->destination = c->control = c->configuration = 0; c->in_flight = false;
    }
    s->configuration = s->raw_tc = s->raw_error = 0;
    s->reads = s->writes = s->errors = s->transfers = s->pending_writes = 0;
    ws53_mdma_irq(s); /* External clock/fault inputs persist. */
}
static void ws53_mdma_realize(DeviceState *dev, Error **errp)
{
    WS53MdmaState *s = WS53_MDMA(dev);
    if (!s->phase_ns || s->phase_ns > 1000000000) { error_setg(errp, "ws53-mdma: phase-ns must be 1..1000000000"); return; }
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-mdma: assumed DTCM mem-to-mem same-width posted-write profile\n");
    }
    qemu_register_reset(ws53_mdma_reset, s);
}
static const Property ws53_mdma_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53MdmaState, assume_profile, false),
    DEFINE_PROP_UINT64("phase-ns", WS53MdmaState, phase_ns, 500),
};
static void ws53_mdma_init(Object *obj)
{
    WS53MdmaState *s = WS53_MDMA(obj); s->clock_on = true;
    memory_region_init_io(&s->mmio, obj, &ws53_mdma_ops, s, TYPE_WS53_MDMA, 0x200);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio); sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    for (unsigned i = 0; i < 8; i++) {
        WS53MdmaChannel *c = &s->channel[i]; c->owner = s; c->index = i;
        c->read_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_mdma_read_beat, c);
        c->write_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_mdma_write_beat, c);
    }
    qdev_init_gpio_in_named(DEVICE(obj), ws53_mdma_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_mdma_stall, "stall", 8);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_mdma_bus_stall, "bus-stall", 8);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_mdma_read_error, "read-error", 8);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_mdma_write_error, "write-error", 8);
    object_property_add_uint64_ptr(obj, "reads", &s->reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "writes", &s->writes, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "errors", &s->errors, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "transfers", &s->transfers, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "pending-writes", &s->pending_writes, OBJ_PROP_FLAG_READ);
}
static void ws53_mdma_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_mdma_realize;
    device_class_set_props(dc, ws53_mdma_properties);
}
static const TypeInfo ws53_mdma_type = {
    .name=TYPE_WS53_MDMA,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53MdmaState),
    .instance_init=ws53_mdma_init,.class_init=ws53_mdma_class_init,
};
