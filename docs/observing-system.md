# The observing system the OSSE imitates

What `tools/obs-archive-osse.py` samples, and where the numbers in it come
from. This is a note about the *real* satellites, kept separately from the code
so that the code can stay a list of constants and this can say why each one is
what it is.

The period the OSSE runs over is July and August 2015, and that pins the fleet:
a mission that launched in 2016 is not available and a mission that ended in
2013 is not either. Sentinel-3A (February 2016) and Jason-3 (January 2016) are
both outside it, which is worth writing down because both are the obvious names
to reach for and `config/layers/obs/adt_3a.yaml` is named after one of them.
That file predates the period being chosen and is kept because the tier 3
archive is pinned to it.

## Why orbits at all

The previous generator drew each altimeter track as a random straight line
across the domain, redrawn every cycle, and gave every point along it a time
drawn uniformly over the assimilation window. Both halves of that are wrong in
ways an OSSE notices.

**A track is not redrawn.** A repeat orbit lays its tracks in the same places
every cycle, so the gaps between them are in the same places too, and a gap
that persists for the length of the experiment is a place the analysis never
sees. Randomising the tracks fills every gap eventually and makes the coverage
uniform, which flatters any assimilation scheme and flatters the ones with long
correlation lengths least. The difference between a scheme that can propagate
information into a persistent void and one that cannot is a large part of what
this OSSE is being run to measure.

**A pass is instantaneous.** A satellite crosses the Gulf of Mexico in about
two minutes. Spreading its observations over a 24 hour window means each one is
compared against a different model state, so a single pass arrives as a
scattering of times rather than a snapshot, and the along-track correlation
that makes altimetry useful is smeared into something a 4D scheme cannot
exploit. This is precisely the error the truth run's sub-window states exist to
avoid, and there is no point recording the truth every six hours if the
observations are then dated at random.

## The altimeters, mid-2015

Four altimeters were flying and producing operational sea level: Jason-2,
SARAL/AltiKa, CryoSat-2 and HY-2A. AVISO published a ground-track animation of
exactly this constellation for June 2015, which is a useful sanity check on the
picture the generator should be producing.

| mission | inclination | repeat | revolutions | equatorial spacing |
|---|---|---|---|---|
| Jason-2 | 66.04 deg | 9.9156 d | 127 | 315 km |
| SARAL/AltiKa | 98.55 deg | 35 d | 501 | 75 km |
| CryoSat-2 | 92 deg | 369 d | 5344 | 7.5 km |
| HY-2A | 99.34 deg | 14 d | 200 | 157 km |

The repeat period and the revolution count are the two numbers everything else
follows from, which is why they are what the generator stores. The nodal period
is `repeat / revolutions`: 112.4 minutes for Jason-2, 100.6 for SARAL, 99.4 for
CryoSat-2, 100.8 for HY-2A, all of which match the published periods. The
equatorial spacing is `360 deg / revolutions` in longitude, which reproduces the
published 315 km and 75 km figures without being told them.

The four are complementary in the way a constellation is meant to be, and each
contributes something different to a Gulf of Mexico experiment:

- **Jason-2** is the reference orbit: coarse in space, dense in time. At 66
  degrees its tracks run noticeably diagonal rather than nearly north-south,
  and 315 km between them at the equator leaves wide gaps that a ten day repeat
  never fills.
- **SARAL/AltiKa** is the opposite: 75 km tracks, but 35 days to lay them all
  down, so on any given day it is sparse and over the experiment it is the one
  that resolves the mesoscale. It is also the most accurate of the four, at Ka
  band with a smaller footprint.
- **CryoSat-2** has a 369 day repeat with a 30 day subcycle, which over a 45
  day experiment behaves as quasi-random dense coverage that never repeats. It
  contributes what a repeat orbit cannot: samples that fall in a different
  place each cycle.
- **HY-2A** is a 14 day repeat between the two extremes.

One caveat carried deliberately rather than modelled: SARAL entered a drifting
phase in July 2016 after its reaction wheels failed, so during the OSSE period
it was still on the exact 35 day repeat. That is the configuration used here.
CryoSat-2's orbit is also not sun-synchronous and not frozen, which is
irrelevant at the fidelity anything below is claimed at.

## The infrared radiometers

Three sensors give a Gulf of Mexico swath in 2015:

| platform | sensor | inclination | period | swath | node |
|---|---|---|---|---|---|
| NOAA-19 | AVHRR/3 | 99.2 deg | 102.0 min | 2900 km | 14:30 asc |
| Metop-B | AVHRR/3 | 98.7 deg | 101.4 min | 2900 km | 09:30 desc |
| Suomi-NPP | VIIRS | 98.7 deg | 101.5 min | 3060 km | 13:30 asc |

All three are sun-synchronous, which is the useful part: the local time of the
ascending node is constant, so a pass over a given longitude happens at the
same local time every day. That single fact gives the generator the diurnal
structure for free. The Gulf sits near 90 W, six hours behind UTC, so NOAA-19's
14:30 local overpass is a little after 20:30 UTC and Metop-B's 09:30 descending
pass is a little after 15:30 UTC. Different platforms therefore observe the
Gulf at genuinely different times of day rather than at random, and a 24 hour
cycle sees each of them roughly twice.

The swaths are wide enough that this is almost a formality: 2900 km against a
domain about 2000 km across means most passes cover the whole Gulf and only the
ones whose ground track is near the domain edge clip it. That is realistic, and
it is the reason the swath edge is modelled at all rather than assuming full
coverage.

NOAA-19's equator crossing time is not actually constant over the mission. It
launched into a 13:45 ascending node and drifted later, and by 2015 was nearer
14:30 to 15:00, which is the value used. The drift is why the mission's
afternoon slot was eventually handed to Suomi-NPP.

**Cloud is the dominant limitation** and is not a detail. An infrared retrieval
needs a clear line of sight, and the summer Gulf of Mexico is convective: the
clear-sky fraction is roughly half, and the cloud is organised into systems
hundreds of kilometres across rather than scattered pixel by pixel. A generator
that thins randomly produces a uniformly degraded swath, which is a
fundamentally easier problem than the real one: what makes cloud hard for data
assimilation is that it removes *contiguous regions* for *days at a time*, so
the background in those regions ages. The gaps are therefore drawn as a
smoothed random field thresholded at the clear-sky fraction, which gives
coherent holes of about the right size, and the field is advected slowly rather
than redrawn, so a hole persists across cycles the way weather does.

Microwave SST (AMSR-2 on GCOM-W, WindSat) sees through cloud and is the real
answer to that gap, at about 50 km resolution and with a wide band along coasts
lost to land contamination. It is not simulated here. Adding it would change
the character of the SST network substantially and is the first thing to add
when this is made more realistic.

## What is fiction

Everything about where the satellites are in their orbits. The generator gives
each platform an ascending node longitude at a fixed epoch, chosen so the sun
synchronous ones have the right local crossing time and otherwise arbitrary. No
two-line element sets are read, so the tracks are a correct repeat pattern with
the correct spacing, laid down in the wrong place. That is the right fidelity
for an OSSE: the sampling *geometry* is what the experiment is sensitive to,
and the absolute phase is not.

Observation errors are representative values, not the mission specifications:
they are what the analysis weights by, so they belong to the data assimilation
configuration rather than to this note. They are set in the generator and in
`config/layers/obs/*.yaml`, and the two must agree.

## Sources

- [Jason-2 orbit, AVISO](https://www.aviso.altimetry.fr/en/missions/past-missions/jason-2/orbit.html)
- [SARAL orbit, AVISO](https://www.aviso.altimetry.fr/en/missions/current-missions/saral/orbit-1.html)
- [CryoSat-2 orbit, AVISO](https://www.aviso.altimetry.fr/en/missions/current-missions/cryosat/orbit.html)
- [Four-satellite ground track animation, June 2015, AVISO](https://www.aviso.altimetry.fr/gallery/entry_180_animation_with_the_4_satellites_grounds_tracks_jason2_saral_altika_cryosat2_hy2a_.html)
- [HY-2A, eoPortal](https://www.eoportal.org/satellite-missions/hy-2a)
- [SARAL, eoPortal](https://www.eoportal.org/satellite-missions/saral)
- [Suomi-NPP, eoPortal](https://www.eoportal.org/satellite-missions/suomi-npp)
