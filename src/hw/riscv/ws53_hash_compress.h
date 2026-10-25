/* Unkeyed compression used by the WS53 HASH model. No padding, implicit IV,
 * key handling or success fixtures: every block transforms the supplied state.
 * SHA: FIPS 180-4 (2015), sections 4/6. SM3: algorithm description in
 * draft-sca-cfrg-sm3-01 (expired Internet-Draft), sections 4/5.
 * These references define algorithms, not WS53 register behavior.
 * SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef WS53_HASH_COMPRESS_H
#define WS53_HASH_COMPRESS_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/* SHA-512 round constants: fractional cube roots of the first 80 primes.
 * The SHA-256 constants are the high 32 bits of the first 64 entries. */
static const uint64_t ws53_hash_k[80] = {
    /* Generated/checked by scripts/ws53_hash_compression_test.py. */
    UINT64_C(0x428a2f98d728ae22), UINT64_C(0x7137449123ef65cd), UINT64_C(0xb5c0fbcfec4d3b2f),
    UINT64_C(0xe9b5dba58189dbbc), UINT64_C(0x3956c25bf348b538), UINT64_C(0x59f111f1b605d019),
    UINT64_C(0x923f82a4af194f9b), UINT64_C(0xab1c5ed5da6d8118), UINT64_C(0xd807aa98a3030242),
    UINT64_C(0x12835b0145706fbe), UINT64_C(0x243185be4ee4b28c), UINT64_C(0x550c7dc3d5ffb4e2),
    UINT64_C(0x72be5d74f27b896f), UINT64_C(0x80deb1fe3b1696b1), UINT64_C(0x9bdc06a725c71235),
    UINT64_C(0xc19bf174cf692694), UINT64_C(0xe49b69c19ef14ad2), UINT64_C(0xefbe4786384f25e3),
    UINT64_C(0x0fc19dc68b8cd5b5), UINT64_C(0x240ca1cc77ac9c65), UINT64_C(0x2de92c6f592b0275),
    UINT64_C(0x4a7484aa6ea6e483), UINT64_C(0x5cb0a9dcbd41fbd4), UINT64_C(0x76f988da831153b5),
    UINT64_C(0x983e5152ee66dfab), UINT64_C(0xa831c66d2db43210), UINT64_C(0xb00327c898fb213f),
    UINT64_C(0xbf597fc7beef0ee4), UINT64_C(0xc6e00bf33da88fc2), UINT64_C(0xd5a79147930aa725),
    UINT64_C(0x06ca6351e003826f), UINT64_C(0x142929670a0e6e70), UINT64_C(0x27b70a8546d22ffc),
    UINT64_C(0x2e1b21385c26c926), UINT64_C(0x4d2c6dfc5ac42aed), UINT64_C(0x53380d139d95b3df),
    UINT64_C(0x650a73548baf63de), UINT64_C(0x766a0abb3c77b2a8), UINT64_C(0x81c2c92e47edaee6),
    UINT64_C(0x92722c851482353b), UINT64_C(0xa2bfe8a14cf10364), UINT64_C(0xa81a664bbc423001),
    UINT64_C(0xc24b8b70d0f89791), UINT64_C(0xc76c51a30654be30), UINT64_C(0xd192e819d6ef5218),
    UINT64_C(0xd69906245565a910), UINT64_C(0xf40e35855771202a), UINT64_C(0x106aa07032bbd1b8),
    UINT64_C(0x19a4c116b8d2d0c8), UINT64_C(0x1e376c085141ab53), UINT64_C(0x2748774cdf8eeb99),
    UINT64_C(0x34b0bcb5e19b48a8), UINT64_C(0x391c0cb3c5c95a63), UINT64_C(0x4ed8aa4ae3418acb),
    UINT64_C(0x5b9cca4f7763e373), UINT64_C(0x682e6ff3d6b2b8a3), UINT64_C(0x748f82ee5defb2fc),
    UINT64_C(0x78a5636f43172f60), UINT64_C(0x84c87814a1f0ab72), UINT64_C(0x8cc702081a6439ec),
    UINT64_C(0x90befffa23631e28), UINT64_C(0xa4506cebde82bde9), UINT64_C(0xbef9a3f7b2c67915),
    UINT64_C(0xc67178f2e372532b), UINT64_C(0xca273eceea26619c), UINT64_C(0xd186b8c721c0c207),
    UINT64_C(0xeada7dd6cde0eb1e), UINT64_C(0xf57d4f7fee6ed178), UINT64_C(0x06f067aa72176fba),
    UINT64_C(0x0a637dc5a2c898a6), UINT64_C(0x113f9804bef90dae), UINT64_C(0x1b710b35131c471b),
    UINT64_C(0x28db77f523047d84), UINT64_C(0x32caab7b40c72493), UINT64_C(0x3c9ebe0a15c9bebc),
    UINT64_C(0x431d67c49c100d4c), UINT64_C(0x4cc5d4becb3e42b6), UINT64_C(0x597f299cfc657e2a),
    UINT64_C(0x5fcb6fab3ad6faec), UINT64_C(0x6c44198c4a475817)
};

static uint32_t ws53_hash_be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | p[3];
}
static uint32_t ws53_hash_swap32(uint32_t x)
{
    return (x >> 24) | ((x >> 8) & 0xff00) |
           ((x << 8) & 0xff0000) | (x << 24);
}
static uint32_t ws53_hash_rotl32(uint32_t x, unsigned n)
{
    n &= 31;
    return n ? (x << n) | (x >> (32 - n)) : x;
}
static uint32_t ws53_hash_rotr32(uint32_t x, unsigned n)
{
    return (x >> n) | (x << (32 - n));
}
static uint64_t ws53_hash_rotr64(uint64_t x, unsigned n)
{
    return (x >> n) | (x << (64 - n));
}
static void ws53_hash_sha1(uint32_t state[16], const uint8_t block[64])
{
    uint32_t h[5], w[80];
    for (unsigned i = 0; i < 5; i++) { h[i] = ws53_hash_swap32(state[i]); }
    for (unsigned i = 0; i < 16; i++) { w[i] = ws53_hash_be32(block + 4 * i); }
    for (unsigned i = 16; i < 80; i++) {
        w[i] = ws53_hash_rotl32(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
    for (unsigned i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20) { f = (b & c) ^ (~b & d); k = 0x5a827999; }
        else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1; }
        else if (i < 60) { f = (b & c) ^ (b & d) ^ (c & d); k = 0x8f1bbcdc; }
        else { f = b ^ c ^ d; k = 0xca62c1d6; }
        uint32_t t = ws53_hash_rotl32(a, 5) + f + e + k + w[i];
        e = d; d = c; c = ws53_hash_rotl32(b, 30); b = a; a = t;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
    for (unsigned i = 0; i < 5; i++) { state[i] = ws53_hash_swap32(h[i]); }
}
static void ws53_hash_sha256(uint32_t state[16], const uint8_t block[64])
{
    uint32_t h[8], w[64];
    for (unsigned i = 0; i < 8; i++) { h[i] = ws53_hash_swap32(state[i]); }
    for (unsigned i = 0; i < 16; i++) { w[i] = ws53_hash_be32(block + 4 * i); }
    for (unsigned i = 16; i < 64; i++) {
        uint32_t x = w[i - 15], y = w[i - 2];
        w[i] = w[i - 16] + (ws53_hash_rotr32(x, 7) ^ ws53_hash_rotr32(x, 18) ^ (x >> 3)) +
               w[i - 7] + (ws53_hash_rotr32(y, 17) ^ ws53_hash_rotr32(y, 19) ^ (y >> 10));
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], z = h[7];
    for (unsigned i = 0; i < 64; i++) {
        uint32_t t = z + (ws53_hash_rotr32(e, 6) ^ ws53_hash_rotr32(e, 11) ^ ws53_hash_rotr32(e, 25)) +
                     ((e & f) ^ (~e & g)) + (uint32_t)(ws53_hash_k[i] >> 32) + w[i];
        uint32_t u = (ws53_hash_rotr32(a, 2) ^ ws53_hash_rotr32(a, 13) ^ ws53_hash_rotr32(a, 22)) +
                     ((a & b) ^ (a & c) ^ (b & c));
        z = g; g = f; f = e; e = d + t; d = c; c = b; b = a; a = t + u;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += z;
    for (unsigned i = 0; i < 8; i++) { state[i] = ws53_hash_swap32(h[i]); }
}
static void ws53_hash_sha512(uint32_t state[16], const uint8_t block[128])
{
    uint64_t h[8], w[80];
    for (unsigned i = 0; i < 8; i++) {
        h[i] = (uint64_t)ws53_hash_swap32(state[2 * i]) << 32 | ws53_hash_swap32(state[2 * i + 1]);
    }
    for (unsigned i = 0; i < 16; i++) {
        w[i] = (uint64_t)ws53_hash_be32(block + 8 * i) << 32 | ws53_hash_be32(block + 8 * i + 4);
    }
    for (unsigned i = 16; i < 80; i++) {
        uint64_t x = w[i - 15], y = w[i - 2];
        w[i] = w[i - 16] + (ws53_hash_rotr64(x, 1) ^ ws53_hash_rotr64(x, 8) ^ (x >> 7)) +
               w[i - 7] + (ws53_hash_rotr64(y, 19) ^ ws53_hash_rotr64(y, 61) ^ (y >> 6));
    }
    uint64_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], z = h[7];
    for (unsigned i = 0; i < 80; i++) {
        uint64_t t = z + (ws53_hash_rotr64(e, 14) ^ ws53_hash_rotr64(e, 18) ^ ws53_hash_rotr64(e, 41)) +
                     ((e & f) ^ (~e & g)) + ws53_hash_k[i] + w[i];
        uint64_t u = (ws53_hash_rotr64(a, 28) ^ ws53_hash_rotr64(a, 34) ^ ws53_hash_rotr64(a, 39)) +
                     ((a & b) ^ (a & c) ^ (b & c));
        z = g; g = f; f = e; e = d + t; d = c; c = b; b = a; a = t + u;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += z;
    for (unsigned i = 0; i < 8; i++) {
        state[2 * i] = ws53_hash_swap32(h[i] >> 32);
        state[2 * i + 1] = ws53_hash_swap32(h[i]);
    }
}
static uint32_t ws53_hash_sm3_p(uint32_t x, unsigned a, unsigned b)
{
    return x ^ ws53_hash_rotl32(x, a) ^ ws53_hash_rotl32(x, b);
}
static void ws53_hash_sm3(uint32_t state[16], const uint8_t block[64])
{
    uint32_t h[8], w[68];
    for (unsigned i = 0; i < 8; i++) { h[i] = ws53_hash_swap32(state[i]); }
    for (unsigned i = 0; i < 16; i++) { w[i] = ws53_hash_be32(block + 4 * i); }
    for (unsigned i = 16; i < 68; i++) {
        w[i] = ws53_hash_sm3_p(w[i - 16] ^ w[i - 9] ^ ws53_hash_rotl32(w[i - 3], 15), 15, 23) ^
               ws53_hash_rotl32(w[i - 13], 7) ^ w[i - 6];
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], z = h[7];
    for (unsigned i = 0; i < 64; i++) {
        uint32_t t = i < 16 ? 0x79cc4519 : 0x7a879d8a;
        uint32_t ar = ws53_hash_rotl32(a, 12);
        uint32_t ss = ws53_hash_rotl32(ar + e + ws53_hash_rotl32(t, i), 7);
        uint32_t ff = i < 16 ? a ^ b ^ c : (a & b) ^ (a & c) ^ (b & c);
        uint32_t gg = i < 16 ? e ^ f ^ g : (e & f) ^ (~e & g);
        uint32_t u = ff + d + (ss ^ ar) + (w[i] ^ w[i + 4]);
        uint32_t v = gg + z + ss + w[i];
        d = c; c = ws53_hash_rotl32(b, 9); b = a; a = u;
        z = g; g = ws53_hash_rotl32(f, 19); f = e; e = ws53_hash_sm3_p(v, 9, 17);
    }
    h[0] ^= a; h[1] ^= b; h[2] ^= c; h[3] ^= d; h[4] ^= e; h[5] ^= f; h[6] ^= g; h[7] ^= z;
    for (unsigned i = 0; i < 8; i++) { state[i] = ws53_hash_swap32(h[i]); }
}
/* Return block size, or zero for an unsupported source algorithm/mode. */
static unsigned ws53_hash_block_size(unsigned algorithm, unsigned mode)
{
    if ((algorithm == 10 || algorithm == 12) && mode == 0) { return 64; }
    if (algorithm == 11 && mode <= 3) { return mode < 2 ? 64 : 128; }
    return 0;
}
static bool ws53_hash_compress(unsigned algorithm, unsigned mode, uint32_t state[16],
                               const uint8_t *data, size_t length)
{
    unsigned block = ws53_hash_block_size(algorithm, mode);
    if (!block || !length || length % block) { return false; }
    for (size_t offset = 0; offset < length; offset += block) {
        if (algorithm == 10) { ws53_hash_sha1(state, data + offset); }
        else if (algorithm == 12) { ws53_hash_sm3(state, data + offset); }
        else if (mode < 2) { ws53_hash_sha256(state, data + offset); }
        else { ws53_hash_sha512(state, data + offset); }
    }
    return true;
}
#endif
