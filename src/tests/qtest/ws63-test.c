/*
 * QTest for the HiSilicon WS63 machine (hw/riscv/ws63.c).
 *
 * Register-level, BOOT-FREE regression: drives the peripheral models directly
 * over MMIO (the test process plays the role of the CPU via qtest_writel/readl)
 * without running any firmware. This complements scripts/smoke-test.sh, which
 * boots real ws63-rs / C-SDK ELFs end-to-end — here we assert the fine-grained
 * register semantics of GPIO, UART, the timer, the interrupt wiring into the
 * WS63 INTC, and a DMA mem->mem round-trip, and they run in milliseconds.
 *
 * Injected into qemu/tests/qtest/ and registered in qtests_riscv32 by
 * scripts/build.sh; run via scripts/qtest.sh (or `meson test ... ws63-test`).
 */
#include "qemu/osdep.h"
#include "libqtest-single.h"

/* Peripheral bases — must match hw/riscv/ws63.c. */
#define TIMER_BASE   0x44002000u
#define UART0_BASE   0x44010000u
#define GPIO0_BASE   0x44028000u
#define DMA0_BASE    0x4A000000u
#define SRAM_BASE    0x00A00000u   /* 0x90000 bytes of system RAM */

/* GPIO register offsets. */
#define GPIO_DATA    0x00   /* pin level (out | external drive), read-only-ish */
#define GPIO_OEN     0x04
#define GPIO_INT_EN  0x0C
#define GPIO_INT_MSK 0x10
#define GPIO_DATA_SET 0x30  /* w1s */
#define GPIO_DATA_CLR 0x34  /* w1c */

/* UART register offsets + status bits. */
#define UART_LINE_STATUS 0x34
#define UART_FIFO_STATUS 0x44
#define FIFO_TX_EMPTY (1u << 1)
#define FIFO_RX_EMPTY (1u << 3)
#define LSR_TX_READY  (3u << 5)

/* Timer: per-channel block at base + 0x100*(ch+1). */
#define T0           (TIMER_BASE + 0x100)
#define TMR_LOAD     0x00
#define TMR_CURRENT  0x08
#define TMR_CONTROL  0x10
#define TMR_EOI      0x14
#define TMR_RAW_ST   0x18
#define TMR_MIS_ST   0x1C
#define TMR_EN       (1u << 0)
#define WS63_IRQ_TIMER0 26

/* DMA channel block: base + 0x100 + ch*0x20. */
#define DMA_CH0      (DMA0_BASE + 0x100)
#define DMA_EN_CHNS  0x10   /* global channel-enable (ChEnReg): the silicon start reg */
#define DMA_ORI_INT_ST (DMA0_BASE + 0x0C)
#define DMA_DST      0x04
#define DMA_CFG      0x08   /* bit0 ch_enable triggers the transfer; fc_tt[9:11] */
#define DMA_SRC      0x10
#define DMA_CTRL     0x14   /* transfersize[0:11], swsize[18:20], dwsize[21:23],
                             * src_inc[26], dest_inc[27], tc_int_en[31] */
#define SDMA_BASE    0x520A0000u   /* secure DMA: unprovisioned on WS63 silicon */

/* Timer cnt_req/cnt_lock handshake bits (CONTROL bits 5/6) — issue #6. */
#define TMR_CNT_REQ  (1u << 5)
#define TMR_CNT_LOCK (1u << 6)

/* ---- GPIO: DATA set/clear, output-enable + interrupt-enable read-back ---- */
static void test_gpio(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    /* DATA_SET pins 0 and 2; DATA reflects the driven level. */
    qtest_writel(qts, GPIO0_BASE + GPIO_DATA_SET, 0x5);
    g_assert_cmphex(qtest_readl(qts, GPIO0_BASE + GPIO_DATA) & 0x5, ==, 0x5);

    /* DATA_CLR pin 0 only -> pin 2 stays high. */
    qtest_writel(qts, GPIO0_BASE + GPIO_DATA_CLR, 0x1);
    g_assert_cmphex(qtest_readl(qts, GPIO0_BASE + GPIO_DATA) & 0x5, ==, 0x4);

    /* OEN / INT_EN / INT_MASK are plain read/write shadow registers. */
    qtest_writel(qts, GPIO0_BASE + GPIO_OEN, 0xF0);
    g_assert_cmphex(qtest_readl(qts, GPIO0_BASE + GPIO_OEN), ==, 0xF0);
    qtest_writel(qts, GPIO0_BASE + GPIO_INT_EN, 0x2);
    g_assert_cmphex(qtest_readl(qts, GPIO0_BASE + GPIO_INT_EN), ==, 0x2);
    qtest_writel(qts, GPIO0_BASE + GPIO_INT_MSK, 0x2);
    g_assert_cmphex(qtest_readl(qts, GPIO0_BASE + GPIO_INT_MSK), ==, 0x2);

    qtest_quit(qts);
}

/* ---- UART: reset-value reads of the FIFO / line-status registers ---- */
static void test_uart(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    /* No chardev attached: TX FIFO empty, RX FIFO empty, TX always ready. */
    g_assert_cmphex(qtest_readl(qts, UART0_BASE + UART_FIFO_STATUS), ==,
                    FIFO_TX_EMPTY | FIFO_RX_EMPTY);
    g_assert_cmphex(qtest_readl(qts, UART0_BASE + UART_LINE_STATUS), ==,
                    LSR_TX_READY);

    qtest_quit(qts);
}

/* ---- Timer: load/enable/decrement, fire, and delivery into the INTC ---- */
static void test_timer_and_intc(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    /* Latch the INTC's input lines so we can observe what the timer delivers. */
    qtest_irq_intercept_in(qts, "/machine/intc");

    qtest_writel(qts, T0 + TMR_LOAD, 100000);
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_LOAD), ==, 100000);

    /* Disabled -> CURRENT reads 0, no interrupt pending. */
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_CURRENT), ==, 0);
    g_assert_false(qtest_get_irq(qts, WS63_IRQ_TIMER0));

    /* Enable (unmasked). The model arms a QEMU_CLOCK_VIRTUAL timer. */
    qtest_writel(qts, T0 + TMR_CONTROL, TMR_EN);
    g_assert_false(qtest_get_irq(qts, WS63_IRQ_TIMER0));

    /* Jump to the timer's deadline -> it fires. */
    qtest_clock_step_next(qts);
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_RAW_ST), ==, 1);
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_MIS_ST), ==, 1);
    g_assert_true(qtest_get_irq(qts, WS63_IRQ_TIMER0));  /* delivered to INTC #26 */

    /* EOI clears the raw status and lowers the line into the INTC. */
    qtest_writel(qts, T0 + TMR_EOI, 1);
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_RAW_ST), ==, 0);
    g_assert_false(qtest_get_irq(qts, WS63_IRQ_TIMER0));

    qtest_quit(qts);
}

/* ---- DMA: channel-0 mem->mem block copy + raw completion status ---- */
static void test_dma_mem2mem(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    const uint32_t src = SRAM_BASE + 0x1000;
    const uint32_t dst = SRAM_BASE + 0x2000;
    const uint32_t pat[4] = {0xdeadbeefu, 0x01234567u, 0xa5a5a5a5u, 0xfeedfaceu};

    for (int i = 0; i < 4; i++) {
        qtest_writel(qts, src + i * 4, pat[i]);
        qtest_writel(qts, dst + i * 4, 0);   /* clear destination */
    }

    /* Program channel 0: dest, src, then ctrl, then cfg (cfg.ch_enable runs it).
     * ctrl: count=4 items, swsize=dwsize=2 (1<<2 = 4 bytes), src_inc + dest_inc. */
    qtest_writel(qts, DMA_CH0 + DMA_DST, dst);
    qtest_writel(qts, DMA_CH0 + DMA_SRC, src);
    qtest_writel(qts, DMA_CH0 + DMA_CTRL,
                 4u | (2u << 18) | (2u << 21) | (1u << 26) | (1u << 27));
    qtest_writel(qts, DMA_CH0 + DMA_CFG, 0x1);  /* ch_enable, fc_tt=0 (mem->mem) */

    for (int i = 0; i < 4; i++) {
        g_assert_cmphex(qtest_readl(qts, dst + i * 4), ==, pat[i]);
    }
    /* Raw transfer-complete status latches channel 0 regardless of IRQ mask. */
    g_assert_cmphex(qtest_readl(qts, DMA_ORI_INT_ST) & 0x1, ==, 0x1);

    qtest_quit(qts);
}

/* ---- DMA: silicon start path — write EN_CHNS, poll EN_CHNS auto-clear (issue #5) ---- */
static void test_dma_en_chns_start(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    const uint32_t src = SRAM_BASE + 0x3000;
    const uint32_t dst = SRAM_BASE + 0x4000;
    const uint32_t pat[4] = {0x11112222u, 0x33334444u, 0x55556666u, 0x77778888u};

    for (int i = 0; i < 4; i++) {
        qtest_writel(qts, src + i * 4, pat[i]);
        qtest_writel(qts, dst + i * 4, 0);
    }

    /* Configure channel 0 WITHOUT CHN_CONFIG.ch_enable — on silicon that bit is
     * config only; the transfer is started by the global EN_CHNS register. */
    qtest_writel(qts, DMA_CH0 + DMA_DST, dst);
    qtest_writel(qts, DMA_CH0 + DMA_SRC, src);
    qtest_writel(qts, DMA_CH0 + DMA_CTRL,
                 4u | (2u << 18) | (2u << 21) | (1u << 26) | (1u << 27));
    qtest_writel(qts, DMA_CH0 + DMA_CFG, 0x0);  /* fc_tt=0, ch_enable NOT set */

    /* Nothing has run yet: EN_CHNS reads 0, destination untouched. */
    g_assert_cmphex(qtest_readl(qts, DMA0_BASE + DMA_EN_CHNS) & 0x1, ==, 0x0);
    g_assert_cmphex(qtest_readl(qts, dst), ==, 0x0);

    /* Start by setting the channel's bit in EN_CHNS (the silicon start). */
    qtest_writel(qts, DMA0_BASE + DMA_EN_CHNS, 0x1);

    for (int i = 0; i < 4; i++) {
        g_assert_cmphex(qtest_readl(qts, dst + i * 4), ==, pat[i]);
    }
    /* Hardware auto-clears the channel's EN_CHNS bit on single-block completion. */
    g_assert_cmphex(qtest_readl(qts, DMA0_BASE + DMA_EN_CHNS) & 0x1, ==, 0x0);
    g_assert_cmphex(qtest_readl(qts, DMA_ORI_INT_ST) & 0x1, ==, 0x1);

    qtest_quit(qts);
}

/* ---- SDMA @0x520A0000 is unprovisioned on WS63 silicon: never runs (issue #7) ---- */
static void test_sdma_unprovisioned(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    const uint32_t src = SRAM_BASE + 0x5000;
    const uint32_t dst = SRAM_BASE + 0x6000;
    qtest_writel(qts, src, 0xcafef00du);
    qtest_writel(qts, dst, 0x0);

    qtest_writel(qts, SDMA_BASE + 0x100 + DMA_DST, dst);
    qtest_writel(qts, SDMA_BASE + 0x100 + DMA_SRC, src);
    qtest_writel(qts, SDMA_BASE + 0x100 + DMA_CTRL,
                 1u | (2u << 18) | (2u << 21) | (1u << 26) | (1u << 27));

    /* Neither start path runs on the secure controller (it would hang on HW). */
    qtest_writel(qts, SDMA_BASE + 0x100 + DMA_CFG, 0x1);  /* CHN_CONFIG.ch_enable */
    qtest_writel(qts, SDMA_BASE + DMA_EN_CHNS, 0x1);       /* EN_CHNS */

    g_assert_cmphex(qtest_readl(qts, dst), ==, 0x0);             /* never copied */
    g_assert_cmphex(qtest_readl(qts, SDMA_BASE + 0x0C) & 0x1, ==, 0x0); /* no done */

    qtest_quit(qts);
}

/* ---- Timer CURRENT_VALUE is a latch refreshed by cnt_req/cnt_lock (issue #6) ---- */
static void test_timer_current_latch(void)
{
    QTestState *qts = qtest_init("-machine ws63");

    qtest_writel(qts, T0 + TMR_LOAD, 100000);
    qtest_writel(qts, T0 + TMR_CONTROL, TMR_EN);

    /* Raw read without the handshake returns the STALE latch (0), not a live
     * count — a live counter would read ~100000 here. This is the gap. */
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_CURRENT), ==, 0);

    /* Handshake: write cnt_req, then cnt_lock reads 1 (snapshot ready). */
    qtest_writel(qts, T0 + TMR_CONTROL, TMR_EN | TMR_CNT_REQ);
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_CONTROL) & TMR_CNT_LOCK, ==, TMR_CNT_LOCK);

    /* Now CURRENT reads a fresh, non-zero snapshot; the read clears cnt_lock. */
    uint32_t snap = qtest_readl(qts, T0 + TMR_CURRENT);
    g_assert_cmpuint(snap, >, 0);
    g_assert_cmpuint(snap, <=, 100000);
    g_assert_cmpuint(qtest_readl(qts, T0 + TMR_CONTROL) & TMR_CNT_LOCK, ==, 0);

    qtest_quit(qts);
}

/* ---------------------------------------------------------------------------
 * Synthetic Wi-Fi/Ethernet MAC (ws63-netmac @ 0x44210000, IRQ 45). A real frame
 * round-trip: bind a SOCK_DGRAM socketpair as the netdev (`-nic socket,fd=`), so
 * one datagram == one Ethernet frame with no length framing. TX path: stage a
 * frame + pulse TX_GO -> read it back off the socket. RX path: send a datagram
 * on the socket -> poll RX_LEN -> read RX_BUF -> assert IRQ status -> RX_ACK.
 * ------------------------------------------------------------------------- */
#define NETMAC_BASE        0x44210000u
#define NETMAC_CTRL        0x000
#define NETMAC_INT_STATUS  0x004
#define NETMAC_INT_CLEAR   0x008
#define NETMAC_TX_LEN      0x00C
#define NETMAC_TX_GO       0x010
#define NETMAC_RX_LEN      0x014
#define NETMAC_RX_ACK      0x018
#define NETMAC_MAC_LO      0x020
#define NETMAC_MAC_HI      0x024
#define NETMAC_TX_BUF      0x1000
#define NETMAC_RX_BUF      0x2000
#define NETMAC_EN          0x1
#define NETMAC_RX_IRQ_EN   0x2
#define NETMAC_INT_RX      0x1

static uint32_t le32(const uint8_t *p)
{
    return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void test_netmac(void)
{
    int sv[2];
    g_assert_cmpint(socketpair(AF_UNIX, SOCK_DGRAM, 0, sv), ==, 0);

    QTestState *qts = qtest_initf("-machine ws63 -nic socket,fd=%d", sv[1]);

    /* enable RX + RX interrupt; staged TX_LEN clamps to the 2048-byte buffer. */
    qtest_writel(qts, NETMAC_BASE + NETMAC_CTRL, NETMAC_EN | NETMAC_RX_IRQ_EN);
    g_assert_cmphex(qtest_readl(qts, NETMAC_BASE + NETMAC_CTRL), ==,
                    NETMAC_EN | NETMAC_RX_IRQ_EN);
    qtest_writel(qts, NETMAC_BASE + NETMAC_TX_LEN, 9999);
    g_assert_cmpuint(qtest_readl(qts, NETMAC_BASE + NETMAC_TX_LEN), ==, 2048);

    /* default MAC must be programmed (qemu_macaddr_default_if_unset). */
    g_assert_cmpuint(qtest_readl(qts, NETMAC_BASE + NETMAC_MAC_LO) |
                     qtest_readl(qts, NETMAC_BASE + NETMAC_MAC_HI), !=, 0);

    /* --- TX: stage a frame, pulse TX_GO, read it back off the socket --- */
    static const int LEN = 64;
    uint8_t tx[64], rx[64];
    for (int i = 0; i < LEN; i++) {
        tx[i] = (uint8_t)(0xA0 + i);
    }
    for (int i = 0; i < LEN; i += 4) {
        qtest_writel(qts, NETMAC_BASE + NETMAC_TX_BUF + i, le32(&tx[i]));
    }
    qtest_writel(qts, NETMAC_BASE + NETMAC_TX_LEN, LEN);
    qtest_writel(qts, NETMAC_BASE + NETMAC_TX_GO, 1);

    g_assert_cmpint(recv(sv[0], rx, sizeof(rx), 0), ==, LEN);
    g_assert_cmpmem(rx, LEN, tx, LEN);

    /* --- RX: inject a datagram, poll RX_LEN, read it back, check IRQ --- */
    uint8_t in[64], out[64];
    for (int i = 0; i < LEN; i++) {
        in[i] = (uint8_t)(0x5A ^ i);
    }
    g_assert_cmpint(send(sv[0], in, LEN, 0), ==, LEN);

    uint32_t rx_len = 0;
    for (int spin = 0; spin < 2000 && rx_len == 0; spin++) {
        rx_len = qtest_readl(qts, NETMAC_BASE + NETMAC_RX_LEN);
    }
    g_assert_cmpuint(rx_len, ==, (uint32_t)LEN);
    g_assert_cmphex(qtest_readl(qts, NETMAC_BASE + NETMAC_INT_STATUS) &
                    NETMAC_INT_RX, ==, NETMAC_INT_RX);

    for (int i = 0; i < LEN; i += 4) {
        uint32_t w = qtest_readl(qts, NETMAC_BASE + NETMAC_RX_BUF + i);
        out[i] = w & 0xFF; out[i + 1] = (w >> 8) & 0xFF;
        out[i + 2] = (w >> 16) & 0xFF; out[i + 3] = (w >> 24) & 0xFF;
    }
    g_assert_cmpmem(out, LEN, in, LEN);

    /* RX_ACK releases the buffer and drops the interrupt. */
    qtest_writel(qts, NETMAC_BASE + NETMAC_RX_ACK, 1);
    g_assert_cmphex(qtest_readl(qts, NETMAC_BASE + NETMAC_INT_STATUS) &
                    NETMAC_INT_RX, ==, 0);

    qtest_quit(qts);
    close(sv[0]);
    close(sv[1]);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    qtest_add_func("/ws63/gpio", test_gpio);
    qtest_add_func("/ws63/uart", test_uart);
    qtest_add_func("/ws63/timer_intc", test_timer_and_intc);
    qtest_add_func("/ws63/timer_current_latch", test_timer_current_latch);
    qtest_add_func("/ws63/dma_mem2mem", test_dma_mem2mem);
    qtest_add_func("/ws63/dma_en_chns_start", test_dma_en_chns_start);
    qtest_add_func("/ws63/sdma_unprovisioned", test_sdma_unprovisioned);
    qtest_add_func("/ws63/netmac", test_netmac);
    return g_test_run();
}
