/* Full-block model primitives. AES uses QEMU's upstream implementation; SM4
 * uses QEMU's upstream S-box/CK with the GB/T 32907 round/key transforms.
 * Modes follow SP800-38A. Functional model only, not constant-time key service.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef WS53_SYMC_BLOCK_H
#define WS53_SYMC_BLOCK_H
#include "crypto/aes.h"
#include "crypto/sm4.h"

static uint32_t ws53_symc_rol(uint32_t x, unsigned n)
{
    return (x << n) | (x >> (32 - n));
}
static uint32_t ws53_symc_subword(uint32_t x)
{
    return (uint32_t)sm4_sbox[x & 255] | (uint32_t)sm4_sbox[(x >> 8) & 255] << 8 |
        (uint32_t)sm4_sbox[(x >> 16) & 255] << 16 | (uint32_t)sm4_sbox[x >> 24] << 24;
}
static void ws53_symc_sm4(const uint8_t key[16], const uint8_t in[16], uint8_t out[16], bool decrypt)
{
    static const uint32_t fk[4] = { 0xa3b1bac6, 0x56aa3350, 0x677d9197, 0xb27022dc };
    uint32_t k[36], rk[32], x[36];
    for (unsigned i = 0; i < 4; i++) {
        k[i] = ldl_be_p(key + 4 * i) ^ fk[i];
        x[i] = ldl_be_p(in + 4 * i);
    }
    for (unsigned i = 0; i < 32; i++) {
        uint32_t t = ws53_symc_subword(k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ sm4_ck[i]);
        rk[i] = k[i + 4] = k[i] ^ t ^ ws53_symc_rol(t, 13) ^ ws53_symc_rol(t, 23);
    }
    for (unsigned i = 0; i < 32; i++) {
        uint32_t t = ws53_symc_subword(x[i + 1] ^ x[i + 2] ^ x[i + 3] ^ rk[decrypt ? 31 - i : i]);
        x[i + 4] = x[i] ^ t ^ ws53_symc_rol(t, 2) ^ ws53_symc_rol(t, 10) ^
                   ws53_symc_rol(t, 18) ^ ws53_symc_rol(t, 24);
    }
    for (unsigned i = 0; i < 4; i++) { stl_be_p(out + 4 * i, x[35 - i]); }
}
static bool ws53_symc_cipher(unsigned algorithm, unsigned size, const uint8_t *key,
                             const uint8_t in[16], uint8_t out[16], bool decrypt)
{
    if (algorithm == 5 && size == 1) { ws53_symc_sm4(key, in, out, decrypt); return true; }
    if (algorithm != 2 || size < 1 || size > 3) { return false; }
    AES_KEY schedule;
    unsigned bits = 64 + 64 * size;
    int result = decrypt ? AES_set_decrypt_key(key, bits, &schedule) : AES_set_encrypt_key(key, bits, &schedule);
    if (result) { return false; }
    if (decrypt) { AES_decrypt(in, out, &schedule); } else { AES_encrypt(in, out, &schedule); }
    return true;
}
static bool ws53_symc_block(unsigned algorithm, unsigned size, unsigned mode, bool decrypt,
                            const uint8_t *key, uint8_t iv[16], const uint8_t in[16], uint8_t out[16])
{
    uint8_t block[16], stream[16];
    if (mode == 1) { return ws53_symc_cipher(algorithm, size, key, in, out, decrypt); }
    if (mode == 3) {
        if (decrypt) {
            if (!ws53_symc_cipher(algorithm, size, key, in, block, true)) { return false; }
            for (unsigned i = 0; i < 16; i++) { out[i] = block[i] ^ iv[i]; }
            memcpy(iv, in, 16);
        } else {
            for (unsigned i = 0; i < 16; i++) { block[i] = in[i] ^ iv[i]; }
            if (!ws53_symc_cipher(algorithm, size, key, block, out, false)) { return false; }
            memcpy(iv, out, 16);
        }
        return true;
    }
    if (mode != 6 && mode != 7 && mode != 8) { return false; }
    if (!ws53_symc_cipher(algorithm, size, key, iv, stream, false)) { return false; }
    for (unsigned i = 0; i < 16; i++) { out[i] = in[i] ^ stream[i]; }
    if (mode == 6) {
        for (int i = 15; i >= 0; i--) { if (++iv[i]) { break; } }
    } else if (mode == 7) { memcpy(iv, stream, 16); }
    else { memcpy(iv, decrypt ? in : out, 16); }
    return true;
}
#endif
