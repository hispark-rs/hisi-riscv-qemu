#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["gmssl==3.2.2", "pycryptodomex==3.23.0"]
# ///
"""Independent references for public fixture keys: PyCryptodome AES + gmssl SM4.

Known answers: NIST SP800-38A Appendix F and draft-ribose-cfrg-sm4-10 A.1/A.2
(the latter is an expired individual draft, not an IETF standard).
No device, production key, padding or authenticated mode is used here.
"""
from Cryptodome.Cipher import AES
from gmssl.sm4 import CryptSM4, SM4_ENCRYPT, SM4_DECRYPT

NIST = 'https://nvlpubs.nist.gov/nistpubs/legacy/sp/nistspecialpublication800-38a.pdf'
SM4_DRAFT = 'https://datatracker.ietf.org/doc/html/draft-ribose-cfrg-sm4-10#appendix-A'
MODES = {1: 'ECB', 3: 'CBC', 6: 'CTR', 7: 'OFB', 8: 'CFB128'}


def xor(a, b): return bytes(x ^ y for x, y in zip(a, b, strict=True))


def reference(algorithm, key, mode, data, iv, decrypt=False):
    assert data and len(data) % 16 == 0 and len(iv) == 16
    assert algorithm in (2, 5) and mode in MODES
    if algorithm == 2:
        opts = {1: (AES.MODE_ECB, {}), 3: (AES.MODE_CBC, dict(iv=iv)),
                6: (AES.MODE_CTR, dict(nonce=b'', initial_value=int.from_bytes(iv, 'big'))),
                7: (AES.MODE_OFB, dict(iv=iv)), 8: (AES.MODE_CFB, dict(iv=iv, segment_size=128))}
        number, kwargs = opts[mode]
        cipher = AES.new(key, number, **kwargs)
        result = cipher.decrypt(data) if decrypt else cipher.encrypt(data)
        if mode in (3, 8): final = data[-16:] if decrypt else result[-16:]
        elif mode == 6: final = ((int.from_bytes(iv, 'big') + len(data)//16) % (1 << 128)).to_bytes(16, 'big')
        elif mode == 7: final = xor(data[-16:], result[-16:])
        else: final = iv
        return result, final
    assert len(key) == 16
    enc, dec = CryptSM4(padding_mode=-1), CryptSM4(padding_mode=-1)
    enc.set_key(key, SM4_ENCRYPT); dec.set_key(key, SM4_DECRYPT)
    result = bytearray(); state = iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset+16]
        if mode == 1: output = (dec if decrypt else enc).crypt_ecb(block)
        elif mode == 3:
            output = xor(dec.crypt_ecb(block), state) if decrypt else enc.crypt_ecb(xor(block, state))
            state = block if decrypt else output
        else:
            stream = enc.crypt_ecb(state); output = xor(block, stream)
            if mode == 6: state = ((int.from_bytes(state, 'big') + 1) % (1 << 128)).to_bytes(16, 'big')
            elif mode == 7: state = stream
            else: state = block if decrypt else output
        result.extend(output)
    return bytes(result), state


def known_answers():
    h = bytes.fromhex
    iv = bytes(range(16))
    plain = h('6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51'
              '30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710')
    key = h('2b7e151628aed2a6abf7158809cf4f3c')
    outputs = {
        1: '3ad77bb40d7a3660a89ecaf32466ef97f5d3d58503b9699de785895a96fdbaaf'
           '43b1cd7f598ece23881b00e3ed0306887b0c785e27e8ad3f8223207104725dd4',
        3: '7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2'
           '73bed6b8e3c1743b7116e69e222295163ff1caa1681fac09120eca307586e1a7',
        6: '874d6191b620e3261bef6864990db6ce9806f66b7970fdff8617187bb9fffdff'
           '5ae4df3edbd5d35e5b4f09020db03eab1e031dda2fbe03d1792170a0f3009cee',
        7: '3b3fd92eb72dad20333449f8e83cfb4a7789508d16918f03f53c52dac54ed825'
           '9740051e9c5fecf64344f7a82260edcc304c6528f659c77866a510d9c1d6ae5e',
        8: '3b3fd92eb72dad20333449f8e83cfb4ac8a64537a0b3a93fcde3cdad9f1ce58b'
           '26751f67a3cbb140b1808cf187a4f4dfc04b05357c5d1c0eeac4c66f9ff7f2e6',
    }
    cases = [(2, key, mode, plain, bytes(range(240,256)) if mode == 6 else iv, h(value)) for mode,value in outputs.items()]
    for keytext, output in [
        ('8e73b0f7da0e6452c810f32b809079e562f8ead2522c6b7b', 'bd334f1d6e45f25ff712a214571fa5cc'),
        ('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4', 'f3eed1bdb5d2a03c064b5a7e3db181f8')]:
        cases.append((2,h(keytext),1,plain[:16],iv,h(output)))
    key = h('0123456789abcdeffedcba9876543210')
    cases.append((5,key,1,key,iv,h('681edf34d206965e86b3e94f536e4246')))
    plain = h('aaaaaaaabbbbbbbbccccccccddddddddeeeeeeeeffffffffaaaaaaaabbbbbbbb')
    outputs = {
        1: '5ec8143de509cff7b5179f8f474b86192f1d305a7fb17df985f81c8482192304',
        3: '78ebb11cc40b0a48312aaeb2040244cb4cb7016951909226979b0d15dc6a8f6d',
        7: 'ac3236cb861dd316e6413b4e3c7524b71d01aca2487ca582cbf5463e6698539b',
        8: 'ac3236cb861dd316e6413b4e3c7524b769d4c54ed433b9a0346009beb37b2b3f',
    }
    cases += [(5,key,mode,plain,iv,h(value)) for mode,value in outputs.items()]
    cases.append((5,key,6,h('aaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbccccccccccccccccdddddddddddddddd'
                           'eeeeeeeeeeeeeeeeffffffffffffffffaaaaaaaaaaaaaaaabbbbbbbbbbbbbbbb'),iv,
                  h('ac3236cb970cc20791364c395a1342d1a3cbc1878c6f30cd074cce385cdd70c7'
                    'f234bc0e24c11980fd1286310ce37b926e02fcd0faa0baf38b2933851d824514')))
    for algorithm,key,mode,data,iv,expected in cases:
        assert reference(algorithm,key,mode,data,iv)[0] == expected, (algorithm,mode)
        assert reference(algorithm,key,mode,expected,iv,True)[0] == data, (algorithm,mode)
    return cases


if __name__ == '__main__':
    print({'known_answer_vectors': len(known_answers()), 'encrypt_and_decrypt': True})
