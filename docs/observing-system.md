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

## How the archive is filed, and the quarter of it that used to be lost

The generator writes fixed time bins, one directory per platform and one file per
bin named by the bin's start, with nothing about an assimilation cycle anywhere
in the layout. `--bin P1D` is what the OSSE archive is built with, and it is an
argument rather than a property of anything downstream: `stage.obs` joins the
bins a window touches and lets ioda apply its own window to the result.
[`src/ackbar/obsarchive.py`](../src/ackbar/obsarchive.py) is the reference for
the selection rule.

This is worth knowing here, and not only in the design, because of what the
previous layout did to the sampling described below. The archive was cut into
assimilation windows at generation time, and an assimilation window is half open,
`(begin, end]`. An observation stamped at the instant a window opens is dropped
by that window, and under a per window archive it sat in no other window's file,
so it was never read by anything. Every platform on a fixed cadence anchored to
the window start lost one sample in four: the gliders, the drifters, and SMAP on
the cycles where its swath crossed the edge. The files held the rows and the
counts below were what the generator drew, so nothing said so.

The counts in this note are what the archive holds and now also what an
experiment reads. Nothing about the sampling changed: the six hourly platforms
are still six hourly, and a sample landing exactly on a window boundary is now
read by the window that boundary closes, exactly once.

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

## The L band radiometer

SMAP, and the choice is against SMOS rather than alongside it. Both were flying
in mid-2015 and either would be defensible. SMAP launched in January 2015 with a
real-aperture feed where SMOS interferes across a sparse array, and it is
markedly less affected by radio frequency interference, which is the thing that
limits L band salinity in a semi-enclosed basin with a populated coast. The Gulf
is exactly that basin.

An 8 day exact repeat in a 685 km sun synchronous orbit, 06:00 descending, with
a 1000 km swath. The archive superobs it onto 40 km, which is the footprint
rather than a choice: the retrieval is an average over that scale, and gridding
it finer would file correlated values as independent ones.

**Its coverage does not collapse under cloud, and that is the entire reason it
earns a place beside the infrared instruments.** It is the only satellite here
that still sees the ocean under a hurricane. What it loses instead is the coast:
a 40 km footprint near land picks up the shore in its sidelobes, and the
retrieval is discarded out to about 100 km, which in this domain removes around
two fifths of the water, including the whole shelf and most of the river plume,
which is where the salinity signal is largest. That is a real property of the
instrument, not a conservative choice in the generator.

The erosion runs from the land in the truth's own mask and **not** from the edge
of the array. The Gulf grid ends in open water on its eastern and southeastern
sides, at the Florida Straits and across the Caribbean, and treating the array
bounds as a coastline drew a second, imaginary shore along them: it cost a
further 989 water cells, seven percent of the basin, none of which has land
within a hundred kilometres. `coastal()` in the generator carries the note.

## The in situ network, and the year it is not from

Twenty five surface drifters, twenty profiling floats, five gliders.

**The float and glider arrays are the best observed period's, not mid-2015's,
and this is the one place the archive deliberately departs from its own
period.** The Gulf in 2015 was chronically under-observed below the surface: at
Argo's design density the deep Gulf supports five to eight floats on a ten day
cycle, under one profile a day in the entire basin, and the 20 to 25 float
arrays over the Loop Current are the UGOS campaign, from 2021 on. The summer
glider lines are likewise a later build-out. An OSSE run against the network
that actually existed answers only how little can be done with almost no
subsurface data, which is not the question being asked.

So any skill number this archive produces is conditional on a network that did
not exist in the year the forcing came from. That is a deliberate and stated
inconsistency, and it belongs in the caption of every figure drawn from these
runs.

The three are different measurements, not three densities of one:

- A **drifter** is a point at 15 cm depth, placed by the ocean. It is advected
  by the truth's own currents, so the array is swept into convergences and wound
  around the Loop Current, and the places it stops sampling are the places an
  analysis would most like it to. Temperature on every hull, conductivity on
  about a fifth of them, which is what the real array carries.
- A **float** gives isolated casts from the deep basin, 5 to 1500 m, on a five
  day cycle, each hull carrying its own phase so the array reports continuously
  rather than all at once. It drifts at parking depth at a small fraction of the
  surface flow, which is what keeps it from being flushed through the Straits of
  Florida in a fortnight.
- A **glider** gives a *section*: steered along waypoints at about 0.25 m s-1,
  returning a continuous slice across whatever the line crosses, casting to
  1000 m once per dive. Over a front that slice is the measurement the front is
  actually in, and no number of scattered casts is the same observation. That is
  why it is a separate layout in the generator and not a float with a shorter
  cycle.

Their declared errors vary with depth, and that is not a refinement. What an in
situ error mostly measures here is representativeness: what a point cast says
about the mean of a 25 km cell. In the seasonal thermocline the vertical
temperature gradient is around 0.1 K per metre, so an eddy displacing an
isotherm by ten metres moves the value by a degree, and a cast declared accurate
to the instrument's own 0.002 K would be weighted hundreds of times more than it
has earned. Below the thermocline the argument runs the other way: a uniform
error would throw away the part of the cast that is genuinely well measured.

**The profiles are potential temperature, not in situ temperature.** The archive
samples the truth's `Temp`, which is MOM6's prognostic, so the observation and
the model field are the same quantity and the operator's whole job is the
vertical interpolation. Assimilating them through SOCA's `InsituTemperature`,
which converts the background before comparing, would put a systematic 0.1 to
0.15 K at a thousand metres into every deep level, against a declared error of
0.05 K there. See the note in `config/obs/obsop_name_map.yml`. If a real float's
in situ reading is wanted, the fix belongs in the generator.

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
- [SMAP, eoPortal](https://www.eoportal.org/satellite-missions/smap)
- [SMAP salinity, NASA PO.DAAC](https://podaac.jpl.nasa.gov/SMAP)
- [Understanding Gulf Ocean Systems (UGOS), NASEM](https://www.nationalacademies.org/gulf/understanding-gulf-ocean-systems)
- [Underwater glider hurricane network, AOML](https://www.aoml.noaa.gov/phod/goos/gliders/)
