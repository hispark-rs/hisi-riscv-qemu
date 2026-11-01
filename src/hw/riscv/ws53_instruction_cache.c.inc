/* Optional functional instruction-cache experiment. SDK163d9150 cache.c.obj
 * supplies CSR command encodings and a 32-byte maintenance granule. Flash
 * cacheability, direct mapping, sequential lookahead and instantaneous
 * maintenance are explicit model assumptions, not silicon geometry/timing.
 * No data cache, memory-attribute/PMP behavior or DMA/coherency capability.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define WS53_ICACHE_FLASH_BASE 0x400000u
#define WS53_ICACHE_FLASH_END  0x800000u

static bool ws53_icache_address(uint32_t address)
{
    return address >= WS53_ICACHE_FLASH_BASE && address < WS53_ICACHE_FLASH_END;
}

static unsigned ws53_icache_index(WS53MachineState *s, uint32_t address)
{
    unsigned lines = 128u << ((s->cache_icctl >> 1) & 3);
    return (address >> 5) & (lines - 1);
}

static WS53InstructionLine *ws53_icache_fill(WS53MachineState *s,
                                          uint32_t address, bool prefetch)
{
    WS53InstructionLine *line = &s->cache_lines[ws53_icache_index(s, address)];

    address &= ~31u;
    if (!line->valid || line->address != address) {
        s->cache_valid += !line->valid;
        line->valid = true;
        line->address = address;
        memcpy(line->data, memory_region_get_ram_ptr(&s->flash) +
               address - WS53_ICACHE_FLASH_BASE, sizeof(line->data));
        s->cache_fills++;
        s->cache_prefetches += prefetch;
    } else if (!prefetch) {
        s->cache_hits++;
    }
    return line;
}

static uint16_t ws53_icache_fetch(CPURISCVState *env, target_ulong address,
                                 uint16_t backing)
{
    WS53MachineState *s = WS53_MACHINE(qdev_get_machine());
    WS53InstructionLine *line;

    if (!(s->cache_icctl & 1) || !ws53_icache_address(address)) {
        return backing;
    }
    if (env != &s->cpu.env || env->priv != PRV_M) {
        error_report("ws53-cache: instruction experiment supports bare M-mode only");
        exit(1);
    }
    s->cache_fetches++;
    line = ws53_icache_fill(s, address, false);
    if (s->cache_aprefi & 1) {
        unsigned count = ((s->cache_aprefi >> 1) & 7) + 1;
        for (unsigned i = 1; i <= count; i++) {
            uint32_t next = (address & ~31u) + i * 32;
            if (ws53_icache_address(next)) {
                ws53_icache_fill(s, next, true);
            }
        }
    }
    /* Instruction halfwords are aligned, so cannot cross a 32-byte line. */
    return lduw_le_p(line->data + (address & 31));
}

static void ws53_icache_invalidate(WS53MachineState *s, bool addressed)
{
    if (addressed) {
        WS53InstructionLine *line =
            &s->cache_lines[ws53_icache_index(s, s->cache_icinva)];
        if (line->valid && line->address == s->cache_icinva) {
            line->valid = false;
            s->cache_valid--;
        }
    } else {
        memset(s->cache_lines, 0, sizeof(s->cache_lines));
        s->cache_valid = 0;
    }
    /* A newly translated TB must consume the modeled instruction bytes.
     * This is not a bus-master drain or a hardware completion poll. */
    tb_flush(CPU(&s->cpu));
}

static void ws53_icache_fence(CPURISCVState *env)
{
    WS53MachineState *s = WS53_MACHINE(qdev_get_machine());
    ws53_icache_invalidate(s, false);
    s->cache_fences++;
}

static RISCVException ws53_icache_op(WS53MachineState *s, CPURISCVState *env,
                                    int csrno, target_ulong *ret_value,
                                    target_ulong value, target_ulong mask,
                                    bool *handled)
{
    uint32_t old, next;
    *handled = true;
    switch (csrno) {
    case 0x7c0:
        old = s->cache_icctl;
        next = (old & ~(uint32_t)mask) | ((uint32_t)value & mask);
        if ((next & ~7u) ||
            (((old ^ next) & 6) && ((old & 1) || s->cache_valid))) {
            return ws53_cache_reject(s, csrno, value, mask,
                                     "unsupported-instruction-size-transition");
        }
        if (old != next) {
            s->cache_icctl = next;
            tb_flush(CPU(&s->cpu));
        }
        break;
    case 0x7c6:
        old = s->cache_aprefi;
        next = (old & ~(uint32_t)mask) | ((uint32_t)value & mask);
        if (next & ~15u) {
            return ws53_cache_reject(s, csrno, value, mask,
                                     "unsupported-instruction-prefetch-bits");
        }
        s->cache_aprefi = next;
        break;
    case 0x7c2:
    case 0x7c4:
        /* SDK only establishes direct, write-only command/address operations;
         * do not invent a readable maintenance value or atomic RMW semantics. */
        if ((uint32_t)mask != UINT32_MAX || ret_value) {
            return ws53_cache_reject(s, csrno, value, mask,
                                     "instruction-command-requires-direct-write");
        }
        if (csrno == 0x7c4) {
            if ((value & 31) || !ws53_icache_address(value)) {
                return ws53_cache_reject(s, csrno, value, mask,
                                         "unsupported-instruction-line-address");
            }
            s->cache_icinva = value;
        } else {
            if (value != 4 && (value != 5 ||
                !ws53_icache_address(s->cache_icinva))) {
                return ws53_cache_reject(s, csrno, value, mask,
                                         "unsupported-instruction-command");
            }
            ws53_icache_invalidate(s, value == 5);
            s->cache_invalidations++;
        }
        return RISCV_EXCP_NONE;
    default:
        *handled = false;
        return RISCV_EXCP_NONE;
    }
    if (ret_value) {
        *ret_value = old;
    }
    return RISCV_EXCP_NONE;
}

static void ws53_icache_patch_get(Object *obj, Visitor *v, const char *name,
                                 void *opaque, Error **errp)
{
    uint64_t value = WS53_MACHINE(obj)->cache_debug_patch;
    visit_type_uint64(v, name, &value, errp);
}

static void ws53_icache_patch_set(Object *obj, Visitor *v, const char *name,
                                 void *opaque, Error **errp)
{
    WS53MachineState *s = WS53_MACHINE(obj);
    uint64_t value;
    uint8_t bytes[4];
    if (!visit_type_uint64(v, name, &value, errp)) {
        return;
    }
    uint32_t address = value >> 32;
    if (runstate_is_running() || !ws53_icache_address(address) || (address & 3)) {
        error_setg(errp, "ws53-cache: debug patch requires stopped CPU and aligned Flash address");
        return;
    }
    stl_le_p(bytes, (uint32_t)value);
    if (address_space_write_rom(&address_space_memory, address,
                               MEMTXATTRS_UNSPECIFIED, bytes, 4) != MEMTX_OK) {
        error_setg(errp, "ws53-cache: debug backing patch failed");
        return;
    }
    /* QEMU invalidates backing TBs, but modeled instruction lines deliberately
     * retain old bytes until ICMAINT, FENCE.I, replacement or model reset. */
    s->cache_debug_patch = value;
}

static void ws53_icache_properties(WS53MachineState *s)
{
    Object *obj = OBJECT(s);
    object_property_add_uint64_ptr(obj, "icache-valid-lines", &s->cache_valid, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "icache-translation-fetches", &s->cache_fetches, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "icache-translation-hits", &s->cache_hits, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "icache-line-fills", &s->cache_fills, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "icache-prefetch-fills", &s->cache_prefetches, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "icache-invalidations", &s->cache_invalidations, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "icache-fence-i", &s->cache_fences, OBJ_PROP_FLAG_READ);
    object_property_add(obj, "icache-debug-patch", "uint64", ws53_icache_patch_get,
                        ws53_icache_patch_set, NULL, NULL);
}
