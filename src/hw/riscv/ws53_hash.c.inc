/* WS53 SPACC/HASH subset, source layout 2e271c8 / SDK 163d9150.
 * Opt-in assumptions: atomic owner claim, selected-bit ACK, two-entry ring,
 * clear5us, descriptor/block dispatch1us, accepted read1us. Accepted reads may
 * survive channel/system reset and clock loss. No physical drain proof, SYMC,
 * HMAC, keyslots, security services or precise hardware latency is implied.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "ws53_hash_compress.h"
#define TYPE_WS53_HASH "ws53-hash"
OBJECT_DECLARE_SIMPLE_TYPE(WS53HashState, WS53_HASH)
enum { HASH_CLEAR_STALL, HASH_DMA_STALL, HASH_BUS_ERROR, HASH_SUPPRESS_DONE,
       HASH_REJECT_CLAIM, HASH_LOSE_OWNER, HASH_ACK_STUCK, HASH_POSTED_HOLD,
       HASH_FAULT_COUNT };
typedef struct WS53HashChannel {
    WS53HashState *parent;
    unsigned number;
    QEMUTimer *clear_timer, *work_timer, *posted_timer;
    uint32_t control, key, base_high, base_low, depth, wp, rp, length;
    uint32_t words[16], index, valid_words, cursor, descriptor_flags;
    uint32_t posted_address, posted_length;
    uint8_t fetched_bytes[128];
    uint64_t epoch, posted_epoch;
    bool active, clearing, posted, fetched, fault[HASH_FAULT_COUNT];
} WS53HashChannel;
struct WS53HashState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    WS53SymcState *symc;
    bool symc_irq;
    WS53HashChannel channels[2];
    bool assume_profile, clock_on;
    uint32_t lock, irq_global, irq_enable, done, clear_done;
    uint32_t cause[2], status[2], pending_reads;
    uint64_t descriptors, bytes_read, transfers, rejections, late_reads;
};
static void ws53_hash_irq(WS53HashState *s)
{
    qemu_set_irq(s->irq, s->clock_on && (s->irq_global & 1) &&
        ((s->assume_profile && (s->done & s->irq_enable & 6)) || s->symc_irq));
}
static void ws53_hash_symc_irq(void *opaque, int n, int level)
{
    WS53HashState *s = opaque; s->symc_irq = level; ws53_hash_irq(s);
}
static void ws53_hash_reject(WS53HashState *s, const char *operation, hwaddr off)
{
    s->rejections++;
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-hash: unimplemented/unavailable %s +0x%" HWADDR_PRIx "\n", operation, off);
}
static bool ws53_hash_range(uint32_t address, uint32_t length)
{
    return length && address >= 0x20010000 && (uint64_t)address + length <= 0x20060000;
}
static unsigned ws53_hash_owner(WS53HashChannel *c)
{
    return (c->parent->lock >> (4 * c->number)) & 15;
}
static void ws53_hash_schedule(WS53HashChannel *c)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (c->posted && !c->fault[HASH_POSTED_HOLD] && !timer_pending(c->posted_timer)) {
        timer_mod(c->posted_timer, now + 1000);
    }
    if (!c->parent->clock_on || !c->parent->assume_profile) { return; }
    if (c->clearing && !c->fault[HASH_CLEAR_STALL] && !timer_pending(c->clear_timer)) {
        timer_mod(c->clear_timer, now + 5000);
    }
    if (c->active && !c->clearing && !c->posted && !c->fault[HASH_DMA_STALL] && !timer_pending(c->work_timer)) {
        timer_mod(c->work_timer, now + 1000);
    }
}
static void ws53_hash_clear_registers(WS53HashChannel *c)
{
    c->control = c->key = c->base_high = c->base_low = c->depth = 0;
    c->wp = c->rp = c->length = c->index = c->valid_words = 0;
    c->cursor = c->descriptor_flags = 0;
    memset(c->words, 0, sizeof(c->words));
    c->active = c->clearing = c->fetched = false;
}
static void ws53_hash_clear_tick(void *opaque)
{
    WS53HashChannel *c = opaque;
    ws53_hash_clear_registers(c);
    c->parent->done &= ~(1u << c->number);
    c->parent->clear_done |= 1u << c->number;
    ws53_hash_irq(c->parent);
}
static void ws53_hash_fail(WS53HashChannel *c, const char *reason)
{
    c->active = false;
    ws53_hash_reject(c->parent, reason, 0x5000 + c->number * 0x100);
    /* No invented bus-error bit or IRQ routing. Missing completion remains
     * observable, and QOM/guest diagnostics identify this modeled failure. */
}
static void ws53_hash_process(WS53HashChannel *c)
{
    WS53HashState *s = c->parent;
    c->fetched = false;
    unsigned algorithm = (c->key >> 16) & 15, mode = (c->key >> 20) & 15;
    if (!ws53_hash_compress(algorithm, mode, c->words, c->fetched_bytes, c->posted_length)) {
        ws53_hash_fail(c, "compression-profile"); return;
    }
    c->cursor += c->posted_length;
    c->length -= c->posted_length;
    if (!c->length) {
        c->rp = (c->rp + 1) % 2;
        c->active = false;
        s->transfers++;
        if ((c->descriptor_flags & 2) && !c->fault[HASH_SUPPRESS_DONE]) {
            s->done |= 1u << c->number;
        }
        ws53_hash_irq(s);
    } else {
        ws53_hash_schedule(c);
    }
}
static void ws53_hash_posted_tick(void *opaque)
{
    WS53HashChannel *c = opaque;
    WS53HashState *s = c->parent;
    MemTxResult result = c->fault[HASH_BUS_ERROR] ? MEMTX_ERROR :
        address_space_read(&address_space_memory, c->posted_address, MEMTXATTRS_UNSPECIFIED,
                           c->fetched_bytes, c->posted_length);
    c->posted = false; s->pending_reads--;
    if (result == MEMTX_OK) { s->bytes_read += c->posted_length; }
    if (c->posted_epoch != c->epoch) {
        s->late_reads++;
        return; /* Accepted old work cannot update the new channel state. */
    }
    if (result != MEMTX_OK) { ws53_hash_fail(c, "DMA-read-error"); return; }
    c->fetched = true;
    /* Completion of the bus read is independent of the HASH computation
     * clock. Keep returned bytes until the compression pipeline can run. */
    if (s->clock_on && !c->fault[HASH_DMA_STALL]) { ws53_hash_process(c); }
}
static void ws53_hash_work_tick(void *opaque)
{
    WS53HashChannel *c = opaque;
    WS53HashState *s = c->parent;
    unsigned block = ws53_hash_block_size((c->key >> 16) & 15, (c->key >> 20) & 15);
    if (!c->active || c->posted || c->clearing || !s->clock_on || c->fault[HASH_DMA_STALL]) { return; }
    if (c->fetched) { ws53_hash_process(c); return; }
    if (!c->length) {
        uint8_t descriptor[16];
        uint64_t address = (uint64_t)c->base_low + 16 * c->rp;
        if (address > UINT32_MAX || !ws53_hash_range(address, 16) || c->fault[HASH_BUS_ERROR] ||
            address_space_read(&address_space_memory, address, MEMTXATTRS_UNSPECIFIED, descriptor, 16) != MEMTX_OK) {
            ws53_hash_fail(c, "descriptor-read-error"); return;
        }
        s->descriptors++;
        uint32_t flags = ldl_le_p(descriptor), length = ldl_le_p(descriptor + 4);
        uint32_t pointer = ldl_le_p(descriptor + 8), high = ldl_le_p(descriptor + 12);
        if ((flags & ~3u) || high || !block || !length || length % block ||
            !ws53_hash_range(pointer, length)) { ws53_hash_fail(c, "descriptor-format"); return; }
        c->descriptor_flags = flags; c->length = length; c->cursor = pointer;
    }
    c->posted = true; s->pending_reads++;
    c->posted_address = c->cursor; c->posted_length = block; c->posted_epoch = c->epoch;
    ws53_hash_schedule(c);
}
static bool ws53_hash_configured(WS53HashChannel *c)
{
    unsigned algorithm = (c->key >> 16) & 15, mode = (c->key >> 20) & 15;
    unsigned block = ws53_hash_block_size(algorithm, mode);
    unsigned words = algorithm == 10 ? 5 : block == 128 ? 16 : 8;
    uint32_t mask = (1u << words) - 1;
    return block && ws53_hash_owner(c) == 1 && (c->control & 0x80000f00) == 0x80000a00 &&
        !(c->key & 0x20f) && c->depth == 2 && !c->base_high && !(c->base_low & 31) &&
        ws53_hash_range(c->base_low, 32) && (c->valid_words & mask) == mask;
}
static void ws53_hash_submit(WS53HashChannel *c, uint32_t value)
{
    if (c->active || c->clearing || c->posted || !ws53_hash_configured(c) ||
        (value & 255) >= 2 || (value & 255) == c->rp ||
        (c->parent->done & (1u << c->number))) {
        ws53_hash_reject(c->parent, "submission", 0x5030 + c->number * 0x100); return;
    }
    c->wp = value; c->length = 0; c->active = true;
    ws53_hash_schedule(c);
}
static uint64_t ws53_hash_read(void *opaque, hwaddr off, unsigned size)
{
    WS53HashState *s = opaque;
    if (s->symc && ws53_symc_handles(off)) { return ws53_symc_read(s->symc, off, size); }
    if (off == 4 && size == 4 && s->clock_on &&
        (s->assume_profile || (s->symc && s->symc->assume_profile))) { return s->irq_global; }
    if (size != 4 || (off & 3) || !s->assume_profile || !s->clock_on) { goto invalid; }
    switch (off) {
    case 4: return s->irq_global;
    case 0x14: return s->clear_done;
    case 0x40: return s->lock;
    case 0x8600: return s->done;
    case 0x8604: return s->irq_enable;
    case 0x8068: return s->cause[0];
    case 0x806c: return s->status[0];
    case 0x8078: return s->cause[1];
    case 0x807c: return s->status[1];
    }
    for (unsigned i = 0; i < 2; i++) {
        WS53HashChannel *c = &s->channels[i];
        hwaddr base = 0x5100 + i * 0x100;
        if (off == 0xa080 + i * 0x80) { return c->words[c->index]; }
        if (off < base || off >= base + 0x100) { continue; }
        switch (off - base) {
        case 0: return c->control;
        case 0x10: return c->key;
        case 0x20: return c->base_high;
        case 0x24: return c->base_low;
        case 0x2c: return c->depth;
        case 0x30: return c->wp;
        case 0x34: return c->rp;
        case 0xc0: return c->length;
        }
    }
invalid:
    ws53_hash_reject(s, "read", off); return 0;
}
static void ws53_hash_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    WS53HashState *s = opaque;
    uint32_t v = value;
    if (s->symc && ws53_symc_handles(off)) { ws53_symc_write(s->symc, off, v, size); return; }
    if (off == 4 && size == 4 && s->clock_on &&
        (s->assume_profile || (s->symc && s->symc->assume_profile))) {
        s->irq_global = v; ws53_hash_irq(s); return;
    }
    if (size != 4 || (off & 3) || !s->assume_profile || !s->clock_on) { goto invalid; }
    if (off == 4) { s->irq_global = v; ws53_hash_irq(s); return; }
    if (off == 0x8604) { s->irq_enable = v; ws53_hash_irq(s); return; }
    if (off == 0x40) {
        uint32_t updated = v & ~0xff0u;
        for (unsigned i = 0; i < 2; i++) {
            WS53HashChannel *c = &s->channels[i];
            unsigned old = ws53_hash_owner(c), owner = (v >> (4 * c->number)) & 15;
            if ((!old && c->fault[HASH_REJECT_CLAIM]) || (old && owner && old != owner) ||
                (!owner && (c->active || c->posted || c->clearing))) { owner = old; }
            updated |= owner << (4 * c->number);
        }
        s->lock = updated; return;
    }
    if (off == 0x14 || off == 0x68 || off == 0x8600) {
        if (v & ~6u) { goto invalid; }
        for (unsigned i = 0; i < 2; i++) {
            WS53HashChannel *c = &s->channels[i];
            uint32_t bit = 1u << c->number;
            if (!(v & bit)) { continue; }
            if (off == 0x68) {
                if (ws53_hash_owner(c) != 1) { ws53_hash_reject(s, "clear-owner", off); continue; }
                timer_del(c->work_timer); timer_del(c->clear_timer);
                c->epoch++; c->active = false; c->clearing = true;
                ws53_hash_schedule(c);
            } else if (!c->fault[HASH_ACK_STUCK]) {
                if (off == 0x14) { s->clear_done &= ~bit; }
                else { s->done &= ~bit; }
            }
        }
        ws53_hash_irq(s); return;
    }
    for (unsigned i = 0; i < 2; i++) {
        WS53HashChannel *c = &s->channels[i];
        hwaddr base = 0x5100 + i * 0x100, state_base = 0xa080 + i * 0x80;
        if ((off >= base && off < base + 0x100) || off == state_base || off == state_base + 4) {
            if (ws53_hash_owner(c) != 1) { goto invalid; }
            if (off == base + 0x30) { ws53_hash_submit(c, v); return; }
            if (off == state_base + 4) {
                if (v >= 16) { goto invalid; }
                c->index = v; return;
            }
            if (c->active || c->clearing || c->posted) { goto invalid; }
            if (off == state_base) { c->words[c->index] = v; c->valid_words |= 1u << c->index; return; }
            switch (off - base) {
            case 0: c->control = v; return;
            case 0x10: c->key = v; return;
            case 0x20: c->base_high = v; return;
            case 0x24: c->base_low = v; return;
            case 0x2c: c->depth = v; return;
            }
            goto invalid;
        }
    }
invalid:
    ws53_hash_reject(s, "write", off);
}
static const MemoryRegionOps ws53_hash_ops = {
    .read = ws53_hash_read, .write = ws53_hash_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};
static void ws53_hash_clock(void *opaque, int n, int level)
{
    WS53HashState *s = opaque; s->clock_on = level;
    for (unsigned i = 0; i < 2; i++) {
        WS53HashChannel *c = &s->channels[i];
        timer_del(c->clear_timer); timer_del(c->work_timer);
        ws53_hash_schedule(c);
    }
    ws53_hash_irq(s);
}
static void ws53_hash_fault(void *opaque, int n, int level)
{
    WS53HashState *s = opaque;
    WS53HashChannel *c = &s->channels[n / HASH_FAULT_COUNT];
    unsigned fault = n % HASH_FAULT_COUNT;
    c->fault[fault] = level;
    if (fault == HASH_LOSE_OWNER && level) {
        s->lock = (s->lock & ~(15u << (4 * c->number))) | (2u << (4 * c->number));
    }
    timer_del(c->clear_timer); timer_del(c->work_timer); timer_del(c->posted_timer);
    ws53_hash_schedule(c);
}
static void ws53_hash_reset(void *opaque)
{
    WS53HashState *s = opaque;
    s->lock = s->irq_global = s->irq_enable = s->done = s->clear_done = 0;
    memset(s->cause, 0, sizeof(s->cause)); memset(s->status, 0, sizeof(s->status));
    s->descriptors = s->bytes_read = s->transfers = s->rejections = s->late_reads = 0;
    for (unsigned i = 0; i < 2; i++) {
        WS53HashChannel *c = &s->channels[i];
        timer_del(c->clear_timer); timer_del(c->work_timer); c->epoch++;
        ws53_hash_clear_registers(c);
        /* Keep accepted reads and external clock/fault inputs alive. */
    }
    ws53_hash_irq(s);
}
static void ws53_hash_realize(DeviceState *dev, Error **errp)
{
    WS53HashState *s = WS53_HASH(dev);
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-hash: assumed two-channel REE/ACK/ring/DMA profile; no ownership or physical drain proof\n");
    }
    qemu_register_reset(ws53_hash_reset, s);
}
static const Property ws53_hash_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53HashState, assume_profile, false),
};
static void ws53_hash_init(Object *obj)
{
    WS53HashState *s = WS53_HASH(obj); s->clock_on = true;
    memory_region_init_io(&s->mmio, obj, &ws53_hash_ops, s, TYPE_WS53_HASH, 0x10000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_hash_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_hash_fault, "fault", 2 * HASH_FAULT_COUNT);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_hash_symc_irq, "symc", 1);
    for (unsigned i = 0; i < 2; i++) {
        WS53HashChannel *c = &s->channels[i]; c->parent = s; c->number = i + 1;
        c->clear_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_hash_clear_tick, c);
        c->work_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_hash_work_tick, c);
        c->posted_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_hash_posted_tick, c);
    }
    object_property_add_uint32_ptr(obj, "pending-reads", &s->pending_reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "inject-ree-cause", &s->cause[1], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-ree-status", &s->status[1], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-tee-cause", &s->cause[0], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-tee-status", &s->status[0], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint64_ptr(obj, "descriptors", &s->descriptors, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "bytes-read", &s->bytes_read, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "transfers", &s->transfers, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "rejections", &s->rejections, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "reads-after-reset", &s->late_reads, OBJ_PROP_FLAG_READ);
}
static void ws53_hash_finalize(Object *obj)
{
    WS53HashState *s = WS53_HASH(obj);
    for (unsigned i = 0; i < 2; i++) {
        timer_free(s->channels[i].clear_timer); timer_free(s->channels[i].work_timer);
        timer_free(s->channels[i].posted_timer);
    }
}
static void ws53_hash_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_hash_realize;
    device_class_set_props(dc, ws53_hash_properties);
}
static const TypeInfo ws53_hash_type = {
    .name = TYPE_WS53_HASH, .parent = TYPE_SYS_BUS_DEVICE, .instance_size = sizeof(WS53HashState),
    .instance_init = ws53_hash_init, .instance_finalize = ws53_hash_finalize, .class_init = ws53_hash_class_init,
};
