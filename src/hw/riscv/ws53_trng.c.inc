/* WS53 unified-security TRNG, SDK 163d9150 / source contract 514615b.
 * Explicit test assumptions: FIFO8, paired production, READY admits two reads,
 * 200us startup then one pair/10us. Deterministic xorshift32 is NOT entropy.
 * No IRQ, IPC service, FRO setup or silicon reset/clock accuracy is claimed.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#define TYPE_WS53_TRNG "ws53-trng"
OBJECT_DECLARE_SIMPLE_TYPE(WS53TrngState, WS53_TRNG)
enum { TRNG_STALL, TRNG_NOT_READY, TRNG_NOT_DONE, TRNG_ZERO, TRNG_ONES,
       TRNG_REPEAT, TRNG_READY_RACE, TRNG_FAULT_COUNT };
struct WS53TrngState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, ring_mmio;
    QEMUTimer *timer;
    uint32_t seed, state, fifo[8], head, count;
    uint16_t ring;
    bool assume_profile, clock_on, fault[TRNG_FAULT_COUNT], status_flip;
    int64_t remaining, deadline;
    uint64_t generated, consumed, discarded, status_reads;
};
static bool ws53_trng_enabled(WS53TrngState *s)
{
    return s->assume_profile && s->clock_on && (s->ring & 1);
}
static void ws53_trng_pause(WS53TrngState *s)
{
    if (timer_pending(s->timer)) {
        s->remaining = MAX(s->deadline - qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(s->timer);
    }
}
static void ws53_trng_resume(WS53TrngState *s)
{
    if (ws53_trng_enabled(s) && !s->fault[TRNG_STALL] && !timer_pending(s->timer)) {
        s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining;
        timer_mod(s->timer, s->deadline);
    }
}
static uint32_t ws53_trng_next(WS53TrngState *s)
{
    uint32_t x = s->state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return s->state = x;
}
static void ws53_trng_tick(void *opaque)
{
    WS53TrngState *s = opaque;
    if (s->count <= 6) {
        uint32_t a = ws53_trng_next(s), b = ws53_trng_next(s);
        /* Fault priority is explicit; no entropy filtering occurs in the device. */
        if (s->fault[TRNG_ZERO]) { a = b = 0; }
        else if (s->fault[TRNG_ONES]) { a = b = UINT32_MAX; }
        else if (s->fault[TRNG_REPEAT]) { b = a; }
        s->fifo[(s->head + s->count++) % 8] = a;
        s->fifo[(s->head + s->count++) % 8] = b;
        s->generated += 2;
    }
    /* Full FIFO applies backpressure; do not advance/drop the test sequence. */
    s->remaining = 10000;
    ws53_trng_resume(s);
}
static uint64_t ws53_trng_read(void *opaque, hwaddr off, unsigned size)
{
    WS53TrngState *s = opaque;
    if (size != 4) { goto invalid; }
    if (off == 0x104) {
        s->status_reads++;
        if (!ws53_trng_enabled(s) || s->count < 2) { return 0; }
        uint32_t value = (!s->fault[TRNG_NOT_READY] ? 1 : 0) |
                         (!s->fault[TRNG_NOT_DONE] ? 2 : 0);
        if (s->fault[TRNG_READY_RACE]) {
            s->status_flip = !s->status_flip;
            if (!s->status_flip) { value = 0; }
        }
        return value;
    }
    if (off == 0x100 && ws53_trng_enabled(s) && s->count) {
        uint32_t value = s->fifo[s->head];
        s->head = (s->head + 1) % 8; s->count--; s->consumed++;
        return value;
    }
invalid:
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-trng: unimplemented/unavailable read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_trng_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    /* FIFO/status are RO; optional FRO selection remains unsupported. */
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-trng: unimplemented write +0x%" HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n", off, v, size);
}
static uint64_t ws53_trng_ring_read(void *opaque, hwaddr off, unsigned size)
{
    WS53TrngState *s = opaque;
    if (!off && size == 2) { return s->ring; }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-trng: unimplemented ring read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}
static void ws53_trng_ring_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53TrngState *s = opaque;
    if (off || size != 2) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-trng: unimplemented ring write +0x%" HWADDR_PRIx " size=%u\n", off, size);
        return;
    }
    bool previous = s->ring & 1;
    s->ring = v;
    if (!!(s->ring & 1) == previous) { return; }
    timer_del(s->timer);
    s->discarded += s->count; s->head = s->count = 0;
    s->status_flip = false; s->remaining = 200000;
    if ((s->ring & 1) && !s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-trng: unimplemented generation without assume-profile\n");
    }
    ws53_trng_resume(s);
}
static const MemoryRegionOps ws53_trng_ops = {
    .read=ws53_trng_read,.write=ws53_trng_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static const MemoryRegionOps ws53_trng_ring_ops = {
    .read=ws53_trng_ring_read,.write=ws53_trng_ring_write,.endianness=DEVICE_LITTLE_ENDIAN,
    .valid={.min_access_size=1,.max_access_size=8,.unaligned=true},
    .impl={.min_access_size=1,.max_access_size=8,.unaligned=true},
};
static void ws53_trng_clock(void *opaque, int n, int level)
{
    WS53TrngState *s = opaque;
    ws53_trng_pause(s); s->clock_on = level; ws53_trng_resume(s);
}
static void ws53_trng_fault(void *opaque, int n, int level)
{
    WS53TrngState *s = opaque;
    ws53_trng_pause(s); s->fault[n] = level;
    if (n == TRNG_READY_RACE) { s->status_flip = false; }
    ws53_trng_resume(s);
}
static void ws53_trng_reset(void *opaque)
{
    WS53TrngState *s = opaque;
    timer_del(s->timer); s->ring = 0; s->state = s->seed;
    s->head = s->count = 0; s->status_flip = false; s->remaining = 200000;
    s->generated = s->consumed = s->discarded = s->status_reads = 0;
    /* Model reset only; externally controlled clock/fault inputs persist. */
}
static void ws53_trng_realize(DeviceState *dev, Error **errp)
{
    WS53TrngState *s = WS53_TRNG(dev);
    if (!s->seed) { error_setg(errp, "ws53-trng: test seed must be nonzero"); return; }
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-trng: assumed FIFO8 pair-ready startup200us pair10us deterministic-test-source; NOT entropy\n");
    }
    qemu_register_reset(ws53_trng_reset, s);
}
static const Property ws53_trng_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53TrngState, assume_profile, false),
    DEFINE_PROP_UINT32("test-seed", WS53TrngState, seed, 1),
};
static void ws53_trng_init(Object *obj)
{
    WS53TrngState *s = WS53_TRNG(obj); s->clock_on = true;
    memory_region_init_io(&s->mmio,obj,&ws53_trng_ops,s,TYPE_WS53_TRNG,0x200);
    memory_region_init_io(&s->ring_mmio,obj,&ws53_trng_ring_ops,s,"ws53-trng-ring",8);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj),&s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj),&s->ring_mmio);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,ws53_trng_tick,s);
    qdev_init_gpio_in_named(DEVICE(obj),ws53_trng_clock,"clock",1);
    qdev_init_gpio_in_named(DEVICE(obj),ws53_trng_fault,"fault",TRNG_FAULT_COUNT);
    object_property_add_uint32_ptr(obj,"fifo-count",&s->count,OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj,"generated",&s->generated,OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj,"consumed",&s->consumed,OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj,"discarded",&s->discarded,OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj,"status-reads",&s->status_reads,OBJ_PROP_FLAG_READ);
}
static void ws53_trng_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_trng_realize;
    device_class_set_props(dc,ws53_trng_properties);
}
static const TypeInfo ws53_trng_type = {
    .name=TYPE_WS53_TRNG,.parent=TYPE_SYS_BUS_DEVICE,.instance_size=sizeof(WS53TrngState),
    .instance_init=ws53_trng_init,.class_init=ws53_trng_class_init,
};
