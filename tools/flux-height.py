#!/usr/bin/env python3
"""How wrong the fluxes are when 2 m scalars are declared at 10 m.

The reference height ACKBAR's data tables declare (`z_bot = 10.0`) is true of
the winds and false of the temperature and humidity, because GEFS and ERA5
publish scalars at 2 m and winds at 10 m and nothing at the other height.
`docs/forcing-reference-height.md` is the write-up; this is where its numbers
come from, so that changing an assumption means rerunning a script rather than
trusting a table.

Three questions, in the order the note asks them:

1. How large is the flux error from declaring 2 m scalars at 10 m.
2. What `z_bot = 2` would cost instead, which is a wind stress error.
3. How well the offline height shift tolerates an approximate SST, which is
   what decides whether the fix can live in the forcing archive rather than in
   a patched `surface_flux.F90`.

The bulk algorithm is a transcription of `ncar_ocean_fluxes` in
`pkg/mom6sis2/src/coupler/surface_flux.F90`, which is the path ACKBAR runs
(`ncar_ocean_flux = .true.`). Stdlib only, so it runs anywhere:

    python3 tools/flux-height.py
"""

import math

# Large and Yeager / FMS `ncar_ocean_fluxes`, plus Monin-Obukhov profiles, so a
# surface layer can be built whose 10 m state is the prescribed one and then
# asked what it looks like at 2 m.
k = 0.4; g = 9.8; cp = 1004.67; Lv = 2.5e6; Rd = 287.04


def qsat(T, p=101325.0):
    es = 611.2 * math.exp(17.67 * (T - 273.15) / (T - 29.65))
    return 0.622 * es / (p - 0.378 * es)


def psi(zeta, kind):
    if zeta > 0:
        return -5 * zeta
    x2 = max(math.sqrt(abs(1 - 16 * zeta)), 1.0); x = math.sqrt(x2)
    if kind == 'm':
        return math.log((1 + 2 * x + x2) * (1 + x2) / 8) - 2 * (math.atan(x) - math.atan(1.0))
    return 2 * math.log((1 + x2) / 2)


def fluxes(u, t, ts, q, qs, z, itts=6):
    """FMS `ncar_ocean_fluxes`: u, t and q all declared at height z.

    The model iterates twice; six is used here so the answer is the algorithm's
    converged one rather than a statement about its iteration count.
    """
    tv = t * (1 + 0.608 * q); u = max(u, 0.5); u10 = u
    cd_n10 = (2.7 / u10 + 0.142 + 0.0764 * u10) / 1e3; rt = math.sqrt(cd_n10)
    ce_n10 = 34.6 * rt / 1e3
    stab = 1.0 if t > ts else 0.0
    ch_n10 = (18.0 * stab + 32.7 * (1 - stab)) * rt / 1e3
    cd, ch, ce = cd_n10, ch_n10, ce_n10
    ustar = tstar = qstar = 0.0
    for _ in range(itts):
        cd_rt = math.sqrt(cd); ustar = cd_rt * u
        tstar = (ch / cd_rt) * (t - ts); qstar = (ce / cd_rt) * (q - qs)
        bstar = g * (tstar / tv + qstar / (q + 1 / 0.608))
        zeta = k * bstar * z / (ustar * ustar)
        zeta = math.copysign(min(abs(zeta), 10.0), zeta)
        pm = psi(zeta, 'm'); ph = psi(zeta, 'h')
        u10 = u / (1 + rt * (math.log(z / 10) - pm) / k)
        cd_n10 = (2.7 / u10 + 0.142 + 0.0764 * u10) / 1e3; rt = math.sqrt(cd_n10)
        ce_n10 = 34.6 * rt / 1e3
        stab = 1.0 if zeta > 0 else 0.0
        ch_n10 = (18.0 * stab + 32.7 * (1 - stab)) * rt / 1e3
        xx = (math.log(z / 10) - pm) / k; cd = cd_n10 / (1 + rt * xx) ** 2
        xx = (math.log(z / 10) - ph) / k
        ch = ch_n10 / (1 + ch_n10 * xx / rt) ** 2
        ce = ce_n10 / (1 + ce_n10 * xx / rt) ** 2
    rho = 101325.0 / (Rd * tv)
    SH = rho * cp * ch * u * (t - ts)
    LH = rho * Lv * ce * u * (q - qs)
    # Positive is ocean to atmosphere, so the model's sign is flipped here.
    return dict(SH=-SH, LH=-LH, ustar=ustar, tstar=tstar, qstar=qstar, zeta=zeta, cd=cd)


def profile_down(u10, t10, q10, ts, qs, zto=2.0):
    """Given the 10 m state, integrate the MO profile down to `zto`.

    The roughness length is never formed: writing the profile at both heights
    and differencing eliminates it, which is why only the 10 m state and the
    stability are needed.
    """
    f = fluxes(u10, t10, ts, q10, qs, 10.0)
    ustar, tstar, qstar, zeta = f['ustar'], f['tstar'], f['qstar'], f['zeta']
    L = 10.0 / zeta if zeta != 0 else 1e9
    ph10 = psi(zeta, 'h'); ph2 = psi(zto / L, 'h') if L != 0 else 0.0
    pm10 = psi(zeta, 'm'); pm2 = psi(zto / L, 'm')
    dt = (tstar / k) * (math.log(zto / 10.0) - ph2 + ph10)
    dq = (qstar / k) * (math.log(zto / 10.0) - ph2 + ph10)
    du = (ustar / k) * (math.log(zto / 10.0) - pm2 + pm10)
    return t10 + dt, q10 + dq, u10 + du, f


def shift_up(u10, t2, q2, ts, iters=12):
    """Invert the profile: from 2 m scalars and an assumed SST, get 10 m ones.

    This is the correction itself, the thing the forcing archive would apply.
    Under-relaxed rather than solved, because it is called with a deliberately
    wrong `ts` below and has to stay well behaved there.
    """
    qs = 0.98 * qsat(ts); t10, q10 = t2, q2
    for _ in range(iters):
        t2g, q2g, _, _ = profile_down(u10, t10, q10, ts, qs)
        t10 += 0.8 * (t2 - t2g)
        q10 += 0.8 * (q2 - q2g)
    return t10, q10


# SST, air temperature, relative humidity, 10 m wind. Two unstable cases, which
# is the Gulf's dominant regime, and two stable ones.
cases = [
    ("unstable: Loop Current, cool dry air", 28.0, 24.0, 0.70, 8.0),
    ("strong cold-air outbreak over Loop", 24.0, 12.0, 0.50, 12.0),
    ("stable: warm moist air over cool shelf", 15.0, 18.0, 0.85, 6.0),
    ("weakly stable shelf, light wind", 20.0, 21.5, 0.85, 4.0),
]


def reference(sst, tair, rh, u10):
    """The self-consistent surface layer for one case, and its 2 m state."""
    ts = sst + 273.15; qs = 0.98 * qsat(ts)
    t10 = tair + 273.15; q10 = rh * qsat(t10)
    t2, q2, _u2, fref = profile_down(u10, t10, q10, ts, qs)
    return ts, qs, t10, q10, t2, q2, fref


print("1. declaring the 2 m scalars at 10 m, which is what the tables do now")
print(f"{'case':42s} {'SH_ref':>8s} {'LH_ref':>8s} {'SH_bias':>8s} {'LH_bias':>8s} "
      f"{'tot':>8s} {'dT':>6s} {'dq g/kg':>8s}")
for name, sst, tair, rh, u10 in cases:
    ts, qs, t10, q10, t2, q2, fref = reference(sst, tair, rh, u10)
    fbad = fluxes(u10, t2, ts, q2, qs, 10.0)
    dSH = fbad['SH'] - fref['SH']; dLH = fbad['LH'] - fref['LH']
    print(f"{name:42s} {fref['SH']:8.1f} {fref['LH']:8.1f} {dSH:8.2f} {dLH:8.2f} "
          f"{dSH + dLH:8.2f} {t2 - t10:6.2f} {(q2 - q10) * 1e3:8.3f}")

print()
print("2. z_bot = 2 instead, which mislabels true 10 m winds as 2 m")
print(f"{'case':42s} {'SH_ref':>7s} {'LH_ref':>7s} {'tau_ref':>8s} | "
      f"{'opt1 dSH':>8s} {'opt1 dLH':>8s} {'opt1 %':>7s} | "
      f"{'opt3 dSH':>8s} {'opt3 dLH':>8s} {'opt3 dtau%':>10s}")
for name, sst, tair, rh, u10 in cases:
    ts, qs, t10, q10, t2, q2, fref = reference(sst, tair, rh, u10)
    rho = 101325.0 / (Rd * t10 * (1 + 0.608 * q10))
    tau_ref = rho * fref['cd'] * max(u10, 0.5) ** 2
    f1 = fluxes(u10, t2, ts, q2, qs, 10.0)
    f3 = fluxes(u10, t2, ts, q2, qs, 2.0)
    tau3 = rho * f3['cd'] * max(u10, 0.5) ** 2
    tot = abs(fref['SH']) + abs(fref['LH'])
    p1 = 100 * ((f1['SH'] - fref['SH']) + (f1['LH'] - fref['LH'])) / tot
    print(f"{name:42s} {fref['SH']:7.1f} {fref['LH']:7.1f} {tau_ref:8.3f} | "
          f"{f1['SH'] - fref['SH']:8.2f} {f1['LH'] - fref['LH']:8.2f} {p1:7.1f} | "
          f"{f3['SH'] - fref['SH']:8.2f} {f3['LH'] - fref['LH']:8.2f} "
          f"{100 * (tau3 - tau_ref) / tau_ref:10.1f}")

print()
print("3. how much an SST error in the shift itself costs, against the")
print("   uncorrected biases in the first table")
for name, sst, tair, rh, u10 in cases:
    ts, qs, t10, q10, t2, q2, fref = reference(sst, tair, rh, u10)
    for err in (0.0, 1.0, -1.0):
        t10h, q10h = shift_up(u10, t2, q2, ts + err)
        f = fluxes(u10, t10h, ts, q10h, qs, 10.0)
        d = (f['SH'] - fref['SH']) + (f['LH'] - fref['LH'])
        print(f"  {name:40s} SSTerr={err:+4.1f}  T10err={t10h - t10:+6.3f} K  "
              f"q10err={(q10h - q10) * 1e3:+6.3f} g/kg  fluxerr={d:+6.2f} W/m2")
