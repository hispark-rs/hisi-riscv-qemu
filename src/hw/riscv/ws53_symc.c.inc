/* WS53 SYMC full-block application profile. SDK 163d9150 / PAC996115a.
 * Explicit assumptions: REE claims, selected ACK, 2-entry rings, 5us clear,
 * 1us pipeline/bus steps, BE full-width CTR increment and final mode IV.
 * Accepted DMA transactions may outlive reset/clear/clock loss. No physical
 * drain, secure handoff, firmware, authentication or timing qualification.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "ws53_symc_block.h"
#define TYPE_WS53_SYMC "ws53-symc"
OBJECT_DECLARE_SIMPLE_TYPE(WS53SymcState, WS53_SYMC)
enum { SYMC_CLEAR_STALL, SYMC_DMA_STALL, SYMC_READ_ERROR, SYMC_WRITE_ERROR,
       SYMC_SUPPRESS_DONE, SYMC_REJECT_CLAIM, SYMC_LOSE_OWNER, SYMC_ACK_STUCK,
       SYMC_POSTED_HOLD, SYMC_FAULT_COUNT };
enum { SYMC_NO_IO, SYMC_READ_IO, SYMC_WRITE_IO };
typedef struct WS53SymcChannel {
    WS53SymcState *parent;
    unsigned number;
    QEMUTimer *clear_timer, *work_timer, *posted_timer;
    uint32_t control, output_control, key, base_high[2], base[2], depth[2], wp[2], rp[2];
    uint32_t remaining, cursor[2], flags, posted_address;
    uint8_t iv[16], input[16], output[16];
    unsigned posted;
    uint64_t epoch, posted_epoch;
    bool active, clearing, fetched, fault[SYMC_FAULT_COUNT];
} WS53SymcChannel;
struct WS53SymcState {
    SysBusDevice parent_obj;
    qemu_irq irq;
    WS53KmState *km;
    WS53SymcChannel channels[2];
    bool assume_profile, clock_on;
    uint32_t lock, irq_enable[2], done[2], clear_done, cause[2], status[2];
    uint32_t pending_reads, pending_writes;
    uint64_t descriptors, bytes_read, bytes_written, transfers, rejections, late_reads, late_writes, reads, writes;
};
static bool ws53_symc_handles(hwaddr off)
{
    return (off >= 8 && off < 12) || (off >= 0x20 && off < 0x24) || (off >= 0x60 && off < 0x64) ||
        (off >= 0x4100 && off < 0x4300) || (off >= 0x8060 && off < 0x8068) ||
        (off >= 0x8070 && off < 0x8078) || (off >= 0x8610 && off < 0x8618) ||
        (off >= 0x9080 && off < 0x9180) || (off >= 0xc000 && off < 0xc008) ||
        (off >= 0xd000 && off < 0xd200);
}
static void ws53_symc_irq(WS53SymcState *s)
{
    /* Global SPACC_IE is owned by the shared HASH/SPACC dispatcher. */
    qemu_set_irq(s->irq, s->assume_profile && s->clock_on &&
        (((s->done[0] & s->irq_enable[0]) | (s->done[1] & s->irq_enable[1])) & 6));
}
static void ws53_symc_reject(WS53SymcState *s, const char *reason, hwaddr off)
{
    s->rejections++;
    qemu_log_mask(LOG_GUEST_ERROR, "ws53-symc: unimplemented/unavailable %s +0x%" HWADDR_PRIx "\n", reason, off);
}
static unsigned ws53_symc_owner(WS53SymcChannel *c)
{
    return (c->parent->lock >> (4 * c->number)) & 15;
}
static bool ws53_symc_range(uint32_t address, uint32_t length)
{
    return length && address >= 0x20010000 && (uint64_t)address + length <= 0x20060000;
}
static bool ws53_symc_overlap(uint32_t a, uint32_t an, uint32_t b, uint32_t bn)
{
    return (uint64_t)a < (uint64_t)b + bn && (uint64_t)b < (uint64_t)a + an;
}
static void ws53_symc_fail(WS53SymcChannel *c, const char *reason)
{
    c->active = false;
    ws53_symc_reject(c->parent, reason, 0x4000 + c->number * 0x100);
    /* No invented cause bit: absence of completion + QOM/guest diagnostic. */
}
static void ws53_symc_schedule(WS53SymcChannel *c)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (c->posted && !c->fault[SYMC_POSTED_HOLD] && !timer_pending(c->posted_timer)) {
        timer_mod(c->posted_timer, now + 1000);
    }
    if (!c->parent->assume_profile || !c->parent->clock_on) { return; }
    if (c->clearing && !c->fault[SYMC_CLEAR_STALL] && !timer_pending(c->clear_timer)) {
        timer_mod(c->clear_timer, now + 5000);
    }
    if (c->active && !c->clearing && !c->posted && !c->fault[SYMC_DMA_STALL] && !timer_pending(c->work_timer)) {
        timer_mod(c->work_timer, now + 1000);
    }
}
static void ws53_symc_clear_registers(WS53SymcChannel *c)
{
    c->control = c->output_control = c->key = c->remaining = c->flags = 0;
    memset(c->base_high, 0, sizeof(c->base_high)); memset(c->base, 0, sizeof(c->base));
    memset(c->depth, 0, sizeof(c->depth)); memset(c->wp, 0, sizeof(c->wp)); memset(c->rp, 0, sizeof(c->rp));
    memset(c->cursor, 0, sizeof(c->cursor)); memset(c->iv, 0, sizeof(c->iv));
    /* Do not erase buffers/posted address belonging to an accepted old write. */
    c->active = c->clearing = c->fetched = false;
}
static void ws53_symc_clear_tick(void *opaque)
{
    WS53SymcChannel *c = opaque; uint32_t mask = 1u << c->number;
    ws53_symc_clear_registers(c);
    c->parent->done[0] &= ~mask; c->parent->done[1] &= ~mask;
    c->parent->clear_done |= mask; ws53_symc_irq(c->parent);
}
static const uint8_t *ws53_symc_key(WS53SymcChannel *c)
{
    WS53KmState *km = c->parent->km;
    unsigned index = c->key & 511, algorithm = (c->key >> 16) & 15, size = (c->key >> 24) & 3;
    unsigned half = (c->flags >> 9) & 1, required = size == 1 ? 1u << half : 3;
    unsigned usage = c->key & (1u << 28) ? 2 : 1;
    if (!km || !km->assume_profile || !km->clock_on || km->loader_error || km->controller_error || index >= 8) { return NULL; }
    WS53KmSlot *slot = &km->slots[index];
    if (slot->owner != 1 || (slot->valid & required) != required || slot->size != size ||
        slot->engine != algorithm * 16 || !(slot->usage & usage) || (half && size != 1)) { return NULL; }
    return slot->key + (size == 1 ? half * 16 : 0);
}
static bool ws53_symc_healthy(WS53SymcChannel *c)
{
    if (ws53_symc_owner(c) != 1 || c->parent->cause[0] || c->parent->cause[1]) {
        ws53_symc_fail(c, "owner-or-error"); return false;
    }
    return true;
}
static void ws53_symc_posted_tick(void *opaque)
{
    WS53SymcChannel *c = opaque; WS53SymcState *s = c->parent;
    bool writing = c->posted == SYMC_WRITE_IO;
    MemTxResult result;
    if (writing) {
        result = c->fault[SYMC_WRITE_ERROR] ? MEMTX_ERROR :
            address_space_write(&address_space_memory, c->posted_address, MEMTXATTRS_UNSPECIFIED, c->output, 16);
        s->pending_writes--;
        if (result == MEMTX_OK) { s->bytes_written += 16; }
    } else {
        result = c->fault[SYMC_READ_ERROR] ? MEMTX_ERROR :
            address_space_read(&address_space_memory, c->posted_address, MEMTXATTRS_UNSPECIFIED, c->input, 16);
        s->pending_reads--;
        if (result == MEMTX_OK) { s->bytes_read += 16; }
    }
    c->posted = SYMC_NO_IO;
    if (c->posted_epoch != c->epoch) {
        if (writing) { s->late_writes++; } else { s->late_reads++; }
        return;
    }
    if (result != MEMTX_OK) { ws53_symc_fail(c, writing ? "DMA-write-error" : "DMA-read-error"); return; }
    if (writing) {
        c->remaining -= 16; c->cursor[0] += 16; c->cursor[1] += 16;
        if (!c->remaining) {
            c->active = false; c->rp[0] = c->rp[1] = 1; s->transfers++;
            if (!c->fault[SYMC_SUPPRESS_DONE]) {
                s->done[0] |= 1u << c->number; s->done[1] |= 1u << c->number;
            }
            ws53_symc_irq(s);
        }
    } else { c->fetched = true; }
    ws53_symc_schedule(c);
}
static bool ws53_symc_fetch(WS53SymcChannel *c)
{
    uint8_t in[32], out[16];
    if (c->fault[SYMC_READ_ERROR] ||
        address_space_read(&address_space_memory, c->base[0], MEMTXATTRS_UNSPECIFIED, in, sizeof(in)) != MEMTX_OK ||
        address_space_read(&address_space_memory, c->base[1], MEMTXATTRS_UNSPECIFIED, out, sizeof(out)) != MEMTX_OK) {
        ws53_symc_fail(c, "descriptor-read-error"); return false;
    }
    c->parent->descriptors += 2;
    uint32_t flags = ldl_le_p(in), length = ldl_le_p(in + 4), source = ldl_le_p(in + 8), dest = ldl_le_p(out + 8);
    if ((flags & ~0x203u) || (flags & 3) != 3 || ldl_le_p(in + 12) || ldl_le_p(out) || ldl_le_p(out + 12) ||
        ldl_le_p(out + 4) != length || !length || (length & 15) || (source & 31) || (dest & 31) ||
        !ws53_symc_range(source, length) || !ws53_symc_range(dest, length) ||
        ws53_symc_overlap(source, length, dest, length) ||
        ws53_symc_overlap(source, length, c->base[0], 64) || ws53_symc_overlap(source, length, c->base[1], 32) ||
        ws53_symc_overlap(dest, length, c->base[0], 64) || ws53_symc_overlap(dest, length, c->base[1], 32)) {
        ws53_symc_fail(c, "descriptor-format"); return false;
    }
    c->flags = flags; c->remaining = length; c->cursor[0] = source; c->cursor[1] = dest;
    memcpy(c->iv, in + 16, 16);
    return true;
}
static void ws53_symc_work_tick(void *opaque)
{
    WS53SymcChannel *c = opaque; WS53SymcState *s = c->parent;
    if (!c->active || c->clearing || c->posted || !s->clock_on || c->fault[SYMC_DMA_STALL]) { return; }
    if (!ws53_symc_healthy(c)) { return; }
    if (!c->remaining && !ws53_symc_fetch(c)) { return; }
    const uint8_t *key = ws53_symc_key(c);
    if (!key) { ws53_symc_fail(c, "key-unavailable"); return; }
    if (c->fetched) {
        c->fetched = false;
        if (!ws53_symc_block((c->key >> 16) & 15, (c->key >> 24) & 3, (c->key >> 20) & 15,
                !!(c->key & (1u << 28)), key, c->iv, c->input, c->output)) {
            ws53_symc_fail(c, "cipher-profile"); return;
        }
        c->posted = SYMC_WRITE_IO; c->posted_address = c->cursor[1]; s->pending_writes++;
    } else {
        c->posted = SYMC_READ_IO; c->posted_address = c->cursor[0]; s->pending_reads++;
    }
    c->posted_epoch = c->epoch; ws53_symc_schedule(c);
}
static bool ws53_symc_configured(WS53SymcChannel *c)
{
    unsigned algorithm = (c->key >> 16) & 15, mode = (c->key >> 20) & 15, size = (c->key >> 24) & 3;
    return ws53_symc_owner(c) == 1 && (c->control & 0x8000ff00) == 0x8000aa00 && !(c->output_control & 3) &&
        ((algorithm == 2 && size >= 1 && size <= 3) || (algorithm == 5 && size == 1)) &&
        (mode == 1 || mode == 3 || mode == 6 || mode == 7 || mode == 8) && !(c->key & 0x0c000000) &&
        (c->depth[0] & 255) == 2 && (c->depth[1] & 255) == 2 && !c->base_high[0] && !c->base_high[1] &&
        !(c->base[0] & 31) && !(c->base[1] & 31) && ws53_symc_range(c->base[0], 64) &&
        ws53_symc_range(c->base[1], 32) && !ws53_symc_overlap(c->base[0], 64, c->base[1], 32);
}
static void ws53_symc_submit(WS53SymcChannel *c, unsigned output, uint32_t value)
{
    if (c->active || c->clearing || c->posted || !ws53_symc_configured(c) || (value & 255) != 1 ||
        (c->wp[output] & 255) || c->rp[output] || (!output && (c->wp[1] & 255) != 1) ||
        ((c->parent->done[0] | c->parent->done[1]) & (1u << c->number))) {
        ws53_symc_reject(c->parent, "submission", (output ? 0xcf30 : 0x4030) + c->number * 0x100); return;
    }
    c->wp[output] = value;
    if (!output) { c->active = true; c->remaining = 0; ws53_symc_schedule(c); }
}
static uint64_t ws53_symc_read(WS53SymcState *s, hwaddr off, unsigned size)
{
    s->reads++;
    if (size != 4 || (off & 3) || !s->assume_profile || !s->clock_on) { goto invalid; }
    switch (off) {
    case 8: return s->clear_done;
    case 0x20: return s->lock;
    case 0x60: return 0; /* command read view, not idle/completion */
    case 0x8060: return s->cause[0]; case 0x8064: return s->status[0];
    case 0x8070: return s->cause[1]; case 0x8074: return s->status[1];
    case 0x8610: return s->done[0]; case 0x8614: return s->irq_enable[0];
    case 0xc000: return s->done[1]; case 0xc004: return s->irq_enable[1];
    }
    for (unsigned i = 0; i < 2; i++) {
        WS53SymcChannel *c = &s->channels[i]; hwaddr iv = 0x9080 + i * 0x80;
        if (off >= iv && off < iv + 16) { return ldl_le_p(c->iv + off - iv); }
        for (unsigned output = 0; output < 2; output++) {
            hwaddr base = (output ? 0xd000 : 0x4100) + i * 0x100;
            if (off < base || off >= base + 0x100) { continue; }
            hwaddr reg = off - base;
            if (!output) {
                if (!reg) { return c->control; } if (reg == 4) { return c->output_control; }
                if (reg == 0x10) { return c->key; }
            }
            if (reg == (output ? 0x28 : 0x2c)) { return c->depth[output]; }
            switch (reg) {
            case 0x20: return c->base_high[output]; case 0x24: return c->base[output];
            case 0x30: return c->wp[output]; case 0x34: return c->rp[output];
            }
        }
    }
invalid: ws53_symc_reject(s, "read", off); return 0;
}
static void ws53_symc_write(WS53SymcState *s, hwaddr off, uint32_t v, unsigned size)
{
    s->writes++;
    if (size != 4 || (off & 3) || !s->assume_profile || !s->clock_on) { goto invalid; }
    if (off == 0x8614 || off == 0xc004) { s->irq_enable[off == 0xc004] = v; ws53_symc_irq(s); return; }
    if (off == 0x20) {
        uint32_t updated = v & ~0xff0u;
        for (unsigned i = 0; i < 2; i++) {
            WS53SymcChannel *c = &s->channels[i]; unsigned old = ws53_symc_owner(c), owner = (v >> (4 * c->number)) & 15;
            if ((!old && c->fault[SYMC_REJECT_CLAIM]) || (old && owner && old != owner) ||
                (!owner && (c->active || c->posted || c->clearing || (c->wp[1] & 255) != c->rp[1]))) { owner = old; }
            updated |= owner << (4 * c->number);
        }
        s->lock = updated; return;
    }
    if (off == 8 || off == 0x60 || off == 0x8610 || off == 0xc000) {
        if (v & ~6u) { goto invalid; }
        for (unsigned i = 0; i < 2; i++) {
            WS53SymcChannel *c = &s->channels[i]; uint32_t mask = 1u << c->number;
            if (!(v & mask)) { continue; }
            if (off == 0x60) {
                if (ws53_symc_owner(c) != 1) { ws53_symc_reject(s, "clear-owner", off); continue; }
                timer_del(c->work_timer); timer_del(c->clear_timer); c->epoch++;
                c->active = false; c->clearing = true; ws53_symc_schedule(c);
            } else if (!c->fault[SYMC_ACK_STUCK]) {
                if (off == 8) { s->clear_done &= ~mask; }
                else { s->done[off == 0xc000] &= ~mask; }
            }
        }
        ws53_symc_irq(s); return;
    }
    for (unsigned i = 0; i < 2; i++) { for (unsigned output = 0; output < 2; output++) {
        WS53SymcChannel *c = &s->channels[i]; hwaddr base = (output ? 0xd000 : 0x4100) + i * 0x100;
        if (off < base || off >= base + 0x100) { continue; }
        hwaddr reg = off - base;
        if (ws53_symc_owner(c) != 1) { goto invalid; }
        if (reg == 0x30) { ws53_symc_submit(c, output, v); return; }
        if (c->active || c->clearing || c->posted || (c->wp[1] & 255) != c->rp[1]) { goto invalid; }
        if (!output) {
            if (!reg) { c->control = v; return; } if (reg == 4) { c->output_control = v; return; }
            if (reg == 0x10) { c->key = v; return; }
        }
        if (reg == (output ? 0x28 : 0x2c)) { c->depth[output] = v; return; }
        if (reg == 0x20) { c->base_high[output] = v; return; }
        if (reg == 0x24) { c->base[output] = v; return; }
        goto invalid;
    }}
invalid: ws53_symc_reject(s, "write", off);
}
static void ws53_symc_clock(void *opaque, int n, int level)
{
    WS53SymcState *s = opaque; s->clock_on = level;
    for (unsigned i = 0; i < 2; i++) {
        WS53SymcChannel *c = &s->channels[i]; timer_del(c->clear_timer); timer_del(c->work_timer); ws53_symc_schedule(c);
    }
    ws53_symc_irq(s);
}
static void ws53_symc_fault(void *opaque, int n, int level)
{
    WS53SymcState *s = opaque; WS53SymcChannel *c = &s->channels[n / SYMC_FAULT_COUNT];
    unsigned fault = n % SYMC_FAULT_COUNT; c->fault[fault] = level;
    if (fault == SYMC_LOSE_OWNER && level) { s->lock = (s->lock & ~(15u << (4 * c->number))) | (2u << (4 * c->number)); }
    timer_del(c->clear_timer); timer_del(c->work_timer); timer_del(c->posted_timer); ws53_symc_schedule(c);
}
static void ws53_symc_reset(void *opaque)
{
    WS53SymcState *s = opaque;
    s->lock = s->clear_done = 0; memset(s->irq_enable, 0, sizeof(s->irq_enable)); memset(s->done, 0, sizeof(s->done));
    memset(s->cause, 0, sizeof(s->cause)); memset(s->status, 0, sizeof(s->status));
    s->descriptors = s->bytes_read = s->bytes_written = s->transfers = s->rejections = s->late_reads = s->late_writes = 0;
    s->reads = s->writes = 0;
    for (unsigned i = 0; i < 2; i++) {
        WS53SymcChannel *c = &s->channels[i]; timer_del(c->clear_timer); timer_del(c->work_timer);
        c->epoch++; ws53_symc_clear_registers(c);
    }
    /* Pending counters and accepted writes survive reset, as do external inputs. */
    ws53_symc_irq(s);
}
static void ws53_symc_realize(DeviceState *dev, Error **errp)
{
    WS53SymcState *s = WS53_SYMC(dev);
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR, "ws53-symc: assumed full-block REE/ring/ACK/DMA profile; no silicon handoff or physical drain proof\n");
    }
    qemu_register_reset(ws53_symc_reset, s);
}
static const Property ws53_symc_properties[] = { DEFINE_PROP_BOOL("assume-profile", WS53SymcState, assume_profile, false) };
static void ws53_symc_init(Object *obj)
{
    WS53SymcState *s = WS53_SYMC(obj); s->clock_on = true;
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_symc_clock, "clock", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_symc_fault, "fault", 2 * SYMC_FAULT_COUNT);
    for (unsigned i = 0; i < 2; i++) {
        WS53SymcChannel *c = &s->channels[i]; c->parent = s; c->number = i + 1;
        c->clear_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_symc_clear_tick, c);
        c->work_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_symc_work_tick, c);
        c->posted_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_symc_posted_tick, c);
    }
    object_property_add_uint32_ptr(obj, "pending-reads", &s->pending_reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "pending-writes", &s->pending_writes, OBJ_PROP_FLAG_READ);
    object_property_add_uint32_ptr(obj, "inject-ree-cause", &s->cause[1], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-ree-status", &s->status[1], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-tee-cause", &s->cause[0], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "inject-tee-status", &s->status[0], OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint64_ptr(obj, "descriptors", &s->descriptors, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "bytes-read", &s->bytes_read, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "bytes-written", &s->bytes_written, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "transfers", &s->transfers, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "rejections", &s->rejections, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "reads", &s->reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "writes", &s->writes, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "reads-after-reset", &s->late_reads, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "writes-after-reset", &s->late_writes, OBJ_PROP_FLAG_READ);
}
static void ws53_symc_finalize(Object *obj)
{
    WS53SymcState *s = WS53_SYMC(obj);
    for (unsigned i = 0; i < 2; i++) {
        timer_free(s->channels[i].clear_timer); timer_free(s->channels[i].work_timer); timer_free(s->channels[i].posted_timer);
    }
}
static void ws53_symc_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc); dc->realize = ws53_symc_realize;
    device_class_set_props(dc, ws53_symc_properties);
}
static const TypeInfo ws53_symc_type = {
    .name = TYPE_WS53_SYMC, .parent = TYPE_SYS_BUS_DEVICE, .instance_size = sizeof(WS53SymcState),
    .instance_init = ws53_symc_init, .instance_finalize = ws53_symc_finalize, .class_init = ws53_symc_class_init,
};
