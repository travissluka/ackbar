# tools/spike/

The ensemble spread investigation: which mechanisms produce spread in this
workflow, and how much. The finding is closed and recorded in the top-level
`CLAUDE.md` ("Ensemble spread does not come from perturbed parameters") and in
`docs/ensemble-spread.md`; these scripts are what produced it and are kept for
reference and for re-measuring if a new spread source is proposed. None of them
are called by the workflow itself.

- `spike-forecast.sh` - one free forecast from a restart set, with parameters
  perturbed. The unit of work every other script here runs in a loop.
- `spike-sweep.py` - sweep MOM6 parameters from one initial condition and
  record what each does. The parameter-perturbation half of the investigation.
- `spike-rank.py` - how many independent directions a perturbation ensemble
  actually spans.
- `spike-spread.py` - measure how much ensemble spread each spike group
  produced, and where.
- `spike-figures.py` - figures for the spread spike: growth curves, depth
  profiles, and maps.
- `spike-stochastic.py` - sweep the NOAA-PSL ocean stochastic physics schemes,
  as seed ensembles. The oSPPT half of the investigation.
- `spike-obc.sh` - one free forecast per boundary member, from one shared
  initial condition. The open-boundary half of the investigation.
- `spike-combined.sh` - all three spread sources at once: a GEFS member, an
  oSPPT pattern, and a draw of perturbed parameters, one combination per
  ensemble member.
- `spike-gefs-fetch.sh` - pull the GEFSv12 reforecast fields needed to force
  MOM6-SIS2, one directory per member.
- `spike-gefs-atm.py` - turn fetched GEFSv12 reforecast GRIB into one `atm.nc`
  per member.
- `spike-gefs-run.sh` - five forecasts from one ocean state, each forced by a
  different GEFS member. The atmospheric-forcing half of the investigation.
