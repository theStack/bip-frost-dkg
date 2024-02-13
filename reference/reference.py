# Reference implementation of BIP DKG. This file is automatically generated by
# reference_py_gen.sh.

from crypto_bip340 import n as GROUP_ORDER, Point, G, point_mul, schnorr_sign, schnorr_verify, tagged_hash, int_from_bytes, bytes_from_int
from crypto_extra import pubkey_gen_plain, point_add_multi, scalar_add_multi, cpoint, xbytes, cbytes, cbytes_ext
from typing import Tuple, List, Optional, Any, Union, Literal
from network import SignerChannel, CoordinatorChannels
from util import *

biptag = "BIP DKG: "

def tagged_hash_bip_dkg(tag: str, msg: bytes) -> bytes:
    return tagged_hash(biptag + tag, msg)

def kdf(seed: bytes, tag: str, extra_input: bytes = b'') -> bytes:
    # TODO: consider different KDF
    return tagged_hash_bip_dkg(tag + "KDF ", seed + extra_input)

# A scalar is represented by an integer modulo GROUP_ORDER
Scalar = int

# A polynomial of degree t - 1 is represented by a list of t coefficients
# f(x) = a[0] + ... + a[t] * x^n
Polynomial = List[Scalar]

# Evaluates polynomial f at x != 0
def polynomial_evaluate(f: Polynomial, x: Scalar) -> Scalar:
   # From a mathematical point of view, there's nothing wrong with evaluating
   # at position 0. But if we try this in a DKG, we may have a catastrophic
   # bug, because we'd compute the implicit secret.
   assert x != 0

   value = 0
   # Reverse coefficients to compute evaluation via Horner's method
   for coeff in f[::-1]:
        value = (value * x) % GROUP_ORDER
        value = (value + coeff) % GROUP_ORDER
   return value

# Returns [f(1), ..., f(n)] for polynomial f with coefficients coeffs
def secret_share_shard(f: Polynomial, n: int) -> List[Scalar]:
    return [polynomial_evaluate(f, x_i) for x_i in range(1, n + 1)]

# A VSS Commitment is a list of points
VSSCommitment = List[Optional[Point]]

# Returns commitments to the coefficients of f
def vss_commit(f: Polynomial) -> VSSCommitment:
    vss_commitment = []
    for coeff in f:
        A_i = point_mul(G, coeff)
        vss_commitment.append(A_i)
    return vss_commitment

def vss_verify(signer_idx: int, share: Scalar, vss_commitment: VSSCommitment) -> bool:
    P = point_mul(G, share)
    Q = [point_mul(vss_commitment[j], pow(signer_idx + 1, j) % GROUP_ORDER) \
         for j in range(0, len(vss_commitment))]
    return P == point_add_multi(Q)

# An extended VSS Commitment is a VSS commitment with a proof of knowledge
VSSCommitmentExt = Tuple[VSSCommitment, bytes]

# A VSS Commitment Sum is the sum of multiple VSS Commitment PoKs
VSSCommitmentSum = Tuple[List[Optional[Point]], List[bytes]]

def serialize_vss_commitment_sum(vss_commitment_sum: VSSCommitmentSum)-> bytes:
    return b''.join([cbytes_ext(P) for P in vss_commitment_sum[0]]) + b''.join(vss_commitment_sum[1])

# Sum the commitments to the i-th coefficients from the given vss_commitments
# for i > 0. This procedure is introduced by Pedersen in section 5.1 of
# 'Non-Interactive and Information-Theoretic Secure Verifiable Secret Sharing'.
def vss_sum_commitments(vss_commitments: List[VSSCommitmentExt], t: int) -> VSSCommitmentSum:
    n = len(vss_commitments)
    assert(all(len(vss_commitment[0]) == t for vss_commitment in vss_commitments))
    first_coefficients = [vss_commitments[i][0][0] for i in range(n)]
    remaining_coeffs_sum = [point_add_multi([vss_commitments[i][0][j] for i in range(n)]) for j in range(1, t)]
    poks = [vss_commitments[i][1] for i in range(n)]
    return (first_coefficients + remaining_coeffs_sum, poks)

# Outputs the shared public key and individual public keys of the participants
def derive_group_info(vss_commitment: VSSCommitment, n: int, t: int) -> Tuple[Optional[Point], List[Optional[Point]]]:
  pk = vss_commitment[0]
  participant_public_keys = []
  for signer_idx in range(0, n):
    pk_i = point_add_multi([point_mul(vss_commitment[j], pow(signer_idx + 1, j) % GROUP_ORDER) \
                            for j in range(0, len(vss_commitment))])
    participant_public_keys += [pk_i]
  return pk, participant_public_keys

SimplPedPopR1State = Tuple[int, int, int]
VSS_PoK_msg = (biptag + "VSS PoK").encode()

def simplpedpop_round1(seed: bytes, t: int, n: int, my_idx: int) -> Tuple[SimplPedPopR1State, VSSCommitmentExt, List[Scalar]]:
    """
    Start SimplPedPop by generating messages to send to the other participants.

    :param bytes seed: FRESH, UNIFORMLY RANDOM 32-byte string
    :param int t: threshold
    :param int n: number of participants
    :param int my_idx:
    :return: a state, a VSS commitment and shares
    """
    assert(t < 2**(4*8))
    coeffs = [int_from_bytes(kdf(seed, "coeffs", i.to_bytes(4, byteorder="big"))) % GROUP_ORDER for i in range(t)]
    assert(my_idx < 2**(4*8))
    # TODO: fix aux_rand
    sig = schnorr_sign(VSS_PoK_msg + my_idx.to_bytes(4, byteorder="big"), bytes_from_int(coeffs[0]), kdf(seed, "VSS PoK"))
    vss_commitment_ext = (vss_commit(coeffs), sig)
    gen_shares = secret_share_shard(coeffs, n)
    state = (t, n, my_idx)
    return state, vss_commitment_ext, gen_shares

DKGOutput = Tuple[Scalar, Optional[Point], List[Optional[Point]]]

def simplpedpop_pre_finalize(state: SimplPedPopR1State,
                         vss_commitments_sum: VSSCommitmentSum, shares_sum: Scalar) \
                         -> Tuple[bytes, DKGOutput]:
    """
    Take the messages received from the participants and pre_finalize the DKG

    :param List[bytes] vss_commitments_sum: output of running vss_sum_commitments() with vss_commitments from all participants (including this participant) (TODO: not a list of bytes)
    :param vss_commitments_sum: TODO
    :param scalar shares_sum: sum of shares received by all participants (including this participant) for this participant mod group order
    :return: the data `eta` that must be input to an equality check protocol, the final share, the shared pubkey, the individual participants' pubkeys
    """
    t, n, my_idx = state
    assert(len(vss_commitments_sum) == 2)
    assert(len(vss_commitments_sum[0]) == n + t - 1)
    assert(len(vss_commitments_sum[1]) == n)

    for i in range(n):
        P_i = vss_commitments_sum[0][i]
        if P_i is None:
            raise InvalidContributionError(i, "Participant sent invalid commitment")
        else:
            pk_i = xbytes(P_i)
            if not schnorr_verify(VSS_PoK_msg + i.to_bytes(4, byteorder="big"), pk_i, vss_commitments_sum[1][i]):
                raise InvalidContributionError(i, "Participant sent invalid proof-of-knowledge")
    # TODO: also add t, n to eta? (and/or the polynomial?)
    eta = serialize_vss_commitment_sum(vss_commitments_sum)
    # Strip the signatures and sum the commitments to the constant coefficients
    vss_commitments_sum_coeffs = [point_add_multi([vss_commitments_sum[0][i] for i in range(n)])] + vss_commitments_sum[0][n:n+t-1]
    if not vss_verify(my_idx, shares_sum, vss_commitments_sum_coeffs):
        raise VSSVerifyError()
    shared_pubkey, signer_pubkeys = derive_group_info(vss_commitments_sum_coeffs, n, t)
    return eta, (shares_sum, shared_pubkey, signer_pubkeys)

def ecdh(deckey: bytes, enckey: bytes, context: bytes) -> Scalar:
    x = int_from_bytes(deckey)
    assert(x != 0)
    Y = cpoint(enckey)
    Z = point_mul(Y, x)
    assert Z is not None
    return int_from_bytes(tagged_hash_bip_dkg("ECDH", cbytes(Z) + context))

def encrypt(share: Scalar, my_deckey: bytes, enckey: bytes, context: bytes) -> Scalar:
    return (share + ecdh(my_deckey, enckey, context)) % GROUP_ORDER

# TODO Add `aggregate` and `decrypt` algorithms for better readability/encapsulation.

EncPedPopR1State = Tuple[int, bytes, List[bytes], SimplPedPopR1State]

def encpedpop_round1(seed: bytes, t: int, n: int, my_deckey: bytes, enckeys: List[bytes], my_idx: int) -> Tuple[EncPedPopR1State, VSSCommitmentExt, List[Scalar]]:
    assert(t < 2**(4*8))
    n = len(enckeys)

    # Protect against reuse of seed in case we previously exported shares
    # encrypted under wrong enckeys.
    enc_context = t.to_bytes(4, byteorder="big") + b''.join(enckeys)
    seed_ = tagged_hash_bip_dkg("EncPedPop seed", seed + enc_context)

    simpl_state, vss_commitment_ext, gen_shares = simplpedpop_round1(seed_, t, n, my_idx)
    assert(len(gen_shares) == n)
    enc_gen_shares : List[Scalar] = []
    for i in range(n):
        try:
            enc_gen_shares.append(encrypt(gen_shares[i], my_deckey, enckeys[i], enc_context))
        except ValueError:  # Invalid enckeys[i]
            raise InvalidContributionError(i, "Participant sent invalid encryption key")
    state1 = (t, my_deckey, enckeys, simpl_state)
    return state1, vss_commitment_ext, enc_gen_shares

def encpedpop_pre_finalize(state1: EncPedPopR1State, vss_commitments_sum: VSSCommitmentSum, enc_shares_sum: Scalar) -> Tuple[bytes, DKGOutput]:
    t, my_deckey, enckeys, simpl_state = state1
    n = len(enckeys)

    assert(len(vss_commitments_sum) == 2)
    assert(len(vss_commitments_sum[0]) == n + t - 1)
    assert(len(vss_commitments_sum[1]) == n)

    enc_context = t.to_bytes(4, byteorder="big") + b''.join(enckeys)
    ecdh_keys = [ecdh(my_deckey, enckeys[i], enc_context) for i in range(n)]
    shares_sum = (enc_shares_sum - scalar_add_multi(ecdh_keys)) % GROUP_ORDER
    eta, dkg_output = simplpedpop_pre_finalize(simpl_state, vss_commitments_sum, shares_sum)
    # TODO: for chilldkg this is unnecessary because the hostpubkeys are already
    # included in eta via setup_id.
    eta += b''.join(enckeys)
    return eta, dkg_output

def chilldkg_hostkey_gen(seed: bytes) -> Tuple[bytes, bytes]:
    my_hostseckey = kdf(seed, "hostseckey")
    my_hostpubkey = pubkey_gen_plain(my_hostseckey)
    return (my_hostseckey, my_hostpubkey)

Setup = Tuple[List[bytes], int, bytes]

def chilldkg_setup_id(hostpubkeys: List[bytes], t: int, context_string: bytes) -> Tuple[Setup, bytes]:
    if len(hostpubkeys) != len(set(hostpubkeys)):
        raise DuplicateHostpubkeyError

    assert(t < 2**(4*8))
    setup_id = tagged_hash("setup id", b''.join(hostpubkeys) + t.to_bytes(4, byteorder="big") + context_string)
    setup = (hostpubkeys, t, setup_id)
    return setup, setup_id

ChillDKGStateR1 = Tuple[Setup, int, EncPedPopR1State]

def chilldkg_round1(seed: bytes, setup: Setup) -> Tuple[ChillDKGStateR1, VSSCommitmentExt, List[Scalar]]:
    my_hostseckey, my_hostpubkey = chilldkg_hostkey_gen(seed)
    (hostpubkeys, t, setup_id) = setup
    n = len(hostpubkeys)

    seed_ = kdf(seed, "setup", setup_id)
    my_idx = hostpubkeys.index(my_hostpubkey)
    enc_state1, vss_commitment_ext, enc_gen_shares = encpedpop_round1(seed_, t, n, my_hostseckey, hostpubkeys, my_idx)
    state1 = (setup, my_idx, enc_state1)
    return state1, vss_commitment_ext, enc_gen_shares

ChillDKGStateR2 = Tuple[Setup, bytes, DKGOutput]

def chilldkg_round2(seed: bytes, state1: ChillDKGStateR1, vss_commitments_sum: VSSCommitmentSum, all_enc_shares_sum: List[Scalar]) -> Tuple[ChillDKGStateR2, bytes]:
    (my_hostseckey, _) = chilldkg_hostkey_gen(seed)
    (setup, my_idx, enc_state1) = state1
    setup_id = setup[2]

    # TODO Not sure if we need to include setup_id as eta here. But it won't hurt.
    # Include the enc_shares in eta to ensure that participants agree on all
    # shares, which in turn ensures that they have the right transcript.
    # TODO This means all parties who hold the "transcript" in the end should
    # participate in Eq?
    my_enc_shares_sum = all_enc_shares_sum[my_idx]
    eta, dkg_output = encpedpop_pre_finalize(enc_state1, vss_commitments_sum, my_enc_shares_sum)
    eta += setup_id + b''.join([bytes_from_int(share) for share in all_enc_shares_sum])
    state2 = (setup, eta, dkg_output)
    return state2, certifying_eq_round1(my_hostseckey, eta)

def chilldkg_finalize(state2: ChillDKGStateR2, cert: bytes) -> Union[DKGOutput, Literal[False]]:
    """
    A return value of False means that `cert` is not a valid certificate.

    You MUST NOT delete `state2` in this case.
    The reason is that some other participant may have a valid certificate and thus deem the DKG run successful.
    That other participant will rely on us not having deleted `state2`.
    Once you obtain that valid certificate, you can call `chilldkg_finalize` again with that certificate.
    """
    (setup, eta, dkg_output) = state2
    hostpubkeys = setup[0]
    if not certifying_eq_finalize(hostpubkeys, eta, cert):
        return False
    return dkg_output

async def chilldkg(chan: SignerChannel, seed: bytes, my_hostseckey: bytes, setup: Setup) -> Union[Tuple[DKGOutput, Any], Literal[False]]:
    state1, vss_commitment_ext, enc_gen_shares = chilldkg_round1(seed, setup)
    chan.send((vss_commitment_ext, enc_gen_shares))
    vss_commitments_sum, all_enc_shares_sum = await chan.receive()

    try:
        state2, eq_round1 = chilldkg_round2(seed, state1, vss_commitments_sum, all_enc_shares_sum)
    except Exception as e:
        print("Exception", repr(e))
        return False

    chan.send(eq_round1)
    cert = await chan.receive()
    dkg_output = chilldkg_finalize(state2, cert)
    if dkg_output == False:
        return False

    transcript = (setup, vss_commitments_sum, all_enc_shares_sum, cert)
    return (dkg_output, transcript)

def certifying_eq_round1(my_hostseckey: bytes, x: bytes) -> bytes:
    # TODO: fix aux_rand
    return schnorr_sign(x, my_hostseckey, b'0'*32)

def verify_cert(hostpubkeys: List[bytes], x: bytes, cert: bytes) -> bool:
    n = len(hostpubkeys)
    if len(cert) != 64*n:
        return False
    is_valid = [schnorr_verify(x, hostpubkeys[i][1:33], cert[i*64:(i+1)*64]) for i in range(n)]
    return all(is_valid)

def certifying_eq_finalize(hostpubkeys: List[bytes], x: bytes, cert: bytes) -> bool:
    return verify_cert(hostpubkeys, x, cert)

async def certifying_eq_coordinate(chans: CoordinatorChannels, hostpubkeys: List[bytes]) -> None:
    n = len(hostpubkeys)
    sigs = []
    for i in range(n):
        sig = await chans.receive_from(i)
        sigs += [sig]
    cert = b''.join(sigs)
    chans.send_all(cert)

async def chilldkg_coordinate(chans: CoordinatorChannels, t: int, hostpubkeys: List[bytes]) -> None:
    n = len(hostpubkeys)
    vss_commitments_ext = []
    all_enc_shares_sum = [0]*n
    for i in range(n):
        vss_commitment_ext, enc_shares = await chans.receive_from(i)
        vss_commitments_ext += [vss_commitment_ext]
        all_enc_shares_sum = [ (all_enc_shares_sum[j] + enc_shares[j]) % GROUP_ORDER for j in range(n) ]
    vss_commitments_sum = vss_sum_commitments(vss_commitments_ext, t)
    chans.send_all((vss_commitments_sum, all_enc_shares_sum))
    await certifying_eq_coordinate(chans, hostpubkeys)

# Recovery requires the seed and the public transcript
def chilldkg_recover(seed: bytes, transcript: Any) -> Union[Tuple[DKGOutput, Setup], Literal[False]]:
    _, my_hostpubkey = chilldkg_hostkey_gen(seed)
    setup, vss_commitments_sum, all_enc_shares_sum, cert = transcript
    hostpubkeys, _, _ = setup
    if not my_hostpubkey in hostpubkeys:
        return False

    state1, _, _ = chilldkg_round1(seed, setup)

    state2, eta  = chilldkg_round2(seed, state1, vss_commitments_sum, all_enc_shares_sum)
    dkg_output = chilldkg_finalize(state2, cert)
    if dkg_output == False:
        return False
    return dkg_output, setup
