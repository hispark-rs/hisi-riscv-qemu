/* WS53 vendor CSR admission, not a cache implementation.
 * SDK 163d9150: arch/cache.h, chip arch_encoding.h, drv_pmp.h and the
 * ws53_liteos_app/libriscv.a cache.c.obj; see docs/reference/ws53-cache-state.md.
 * Historical halted Flash application: ICCTL=6, DCCTL=0, MEMATTRL=0x33333333.
 * These are neither silicon reset values nor proof of clean aliases or drain.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define WS53_CACHE_OBSERVED "observed-flash-baseline"
#define WS53_CACHE_UNKNOWN "unknown"

static const char *ws53_cache_profile(WS53MachineState *s)
{
    return s->cache_observed ? WS53_CACHE_OBSERVED : WS53_CACHE_UNKNOWN;
}

static RISCVException ws53_cache_predicate(CPURISCVState *env, int csrno)
{
    Object *machine = qdev_get_machine();

    /* The CSR table is process-global; only the WS53 board installs these ops.
     * Still bind each access to its application CPU, not a global CSR shadow. */
    if (!object_dynamic_cast(machine, TYPE_WS53_MACHINE) ||
        env != &WS53_MACHINE(machine)->cpu.env) {
        return RISCV_EXCP_ILLEGAL_INST;
    }
    return RISCV_EXCP_NONE;
}

static RISCVException ws53_cache_reject(WS53MachineState *s, int csrno,
                                      target_ulong value, target_ulong mask,
                                      const char *reason)
{
    s->cache_rejections++;
    s->cache_epoch_rejections++;
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-cache: unsupported csr=0x%03x op=%s value=0x%08x mask=0x%08x "
        "profile=%s reason=%s (model limitation, not silicon illegal)\n",
        csrno, mask ? "write" : "read", (uint32_t)value, (uint32_t)mask,
        ws53_cache_profile(s), reason);
    return RISCV_EXCP_ILLEGAL_INST;
}

static RISCVException ws53_cache_op(CPURISCVState *env, int csrno,
                                   target_ulong *ret_value,
                                   target_ulong value, target_ulong mask)
{
    WS53MachineState *s = WS53_MACHINE(qdev_get_machine());
    uint32_t old, next;
    const char *reason;

    if (!s->cache_observed) {
        return ws53_cache_reject(s, csrno, value, mask,
                                 "unknown-inherited-state");
    }
    switch (csrno) {
    case 0x7c0:
        old = s->cache_icctl;
        reason = "instruction-cache-change-not-modeled";
        break;
    case 0x7c1:
        old = s->cache_dcctl;
        reason = "data-cache-change-not-modeled";
        break;
    case 0x7d8:
        old = s->cache_memattrl;
        reason = "memory-attribute-change-not-modeled";
        break;
    case 0x7d9:
        return ws53_cache_reject(s, csrno, value, mask, "unobserved-memattrh");
    case 0x7c2: case 0x7c3:
        return ws53_cache_reject(s, csrno, value, mask,
                                 "maintenance-not-modeled");
    case 0x7c4: case 0x7c5:
        return ws53_cache_reject(s, csrno, value, mask,
                                 "maintenance-address-not-modeled");
    case 0x7c6: case 0x7c7:
        return ws53_cache_reject(s, csrno, value, mask, "prefetch-not-modeled");
    default:
        return ws53_cache_reject(s, csrno, value, mask, "vendor-csr-not-modeled");
    }
    next = (old & ~(uint32_t)mask) | ((uint32_t)value & (uint32_t)mask);
    if (mask && next != old) {
        /* Reject the complete RMW atomically. Neither the CSR nor rd may be
         * updated. In particular, do not accept DCEN=1 then use coherent RAM. */
        return ws53_cache_reject(s, csrno, value, mask, reason);
    }
    /* A same-value write is a state-only model assumption. It does not flush,
     * invalidate, complete maintenance, drain another master or reset taint. */
    if (ret_value) {
        *ret_value = old;
    }
    return RISCV_EXCP_NONE;
}

static riscv_csr_operations ws53_cache_ops = {
    .name = "ws53-cache-state",
    .predicate = ws53_cache_predicate,
    .op = ws53_cache_op,
};

static void ws53_cache_state_reset(WS53MachineState *s)
{
    /* Reapply the selected experiment's initial state, not a silicon reset.
     * Unknown profiles have no readable initial values. Lifetime rejections
     * survive reset; the per-run diagnostic count starts a new epoch. */
    s->cache_epoch_rejections = 0;
    if (s->cache_observed) {
        s->cache_icctl = 6;
        s->cache_dcctl = 0;
        s->cache_memattrl = 0x33333333;
    }
}

static void ws53_cache_state_init(WS53MachineState *s)
{
    for (unsigned csrno = 0x7c0; csrno <= 0x7ff; csrno++) {
        riscv_set_csr_ops(csrno, &ws53_cache_ops);
    }
    ws53_cache_state_reset(s);
    object_property_add_uint64_ptr(OBJECT(s), "cache-csr-rejections",
                                  &s->cache_rejections, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(OBJECT(s), "cache-csr-epoch-rejections",
                                  &s->cache_epoch_rejections, OBJ_PROP_FLAG_READ);
    if (s->cache_observed) {
        error_report("ws53-cache: observed Flash CSR profile ICCTL=6 DCCTL=0 "
                     "MEMATTRL=0x33333333; state only, no cache/coherency/drain");
    }
}

static char *ws53_get_cache_profile(Object *obj, Error **errp)
{
    return g_strdup(ws53_cache_profile(WS53_MACHINE(obj)));
}

static void ws53_set_cache_profile(Object *obj, const char *value, Error **errp)
{
    WS53MachineState *s = WS53_MACHINE(obj);

    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        error_setg(errp, "ws53: cache-state-profile is a startup-only property");
    } else if (!strcmp(value, WS53_CACHE_UNKNOWN)) {
        s->cache_observed = false;
    } else if (!strcmp(value, WS53_CACHE_OBSERVED)) {
        s->cache_observed = true;
    } else {
        error_setg(errp, "ws53: cache-state-profile must be unknown or "
                   WS53_CACHE_OBSERVED);
    }
}

static bool ws53_get_cache_tainted(Object *obj, Error **errp)
{
    return WS53_MACHINE(obj)->cache_epoch_rejections != 0;
}

static void ws53_cache_state_class_init(ObjectClass *oc)
{
    object_class_property_add_str(oc, "cache-state-profile",
                                 ws53_get_cache_profile, ws53_set_cache_profile);
    object_class_property_set_description(oc, "cache-state-profile",
        "unknown (default) rejects vendor CSR access; observed-flash-baseline "
        "admits only historical ICCTL/DCCTL/MEMATTRL state, not DMA safety");
    object_class_property_add_bool(oc, "cache-csr-tainted",
                                  ws53_get_cache_tainted, NULL);
    object_class_property_set_description(oc, "cache-csr-tainted",
        "At least one unsupported vendor CSR operation in this model reset "
        "epoch; false is not a cache/coherency/drain qualification");
}
