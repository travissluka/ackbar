# MOM6-SIS2 input data

Where the model's static input data comes from, how it is wired up here, and what else is
available if we need more.

## How MOM6-examples expects data to be laid out

Nothing in `INPUT/` is committed to the repo. Each config's `INPUT/` directory is a set of
relative symlinks through a `.datasets` link at the repo root, e.g.

```
ice_ocean_SIS2/OM_1deg/INPUT/.datasets      -> ../../../.datasets
ice_ocean_SIS2/OM_1deg/INPUT/grid_spec.nc   -> .datasets/OM_1deg/INPUT/grid_spec.nc
ice_ocean_SIS2/OM_1deg/INPUT/JRA_tas.nc     -> .datasets/reanalysis/JRA55-do/v1.4.0/short_sample/tas_JRA_sample.nc
ice_ocean_SIS2/OM_1deg/INPUT/woa13_*.nc     -> .datasets/obs/NOAA-NODC/WOA13/v2a/woa13_*.nc
```

So the whole job is: point `<clone>/.datasets` at a tree whose top-level directories match
the tarball names below.

Check a config is fully satisfied with:

```bash
find -L ice_ocean_SIS2/OM_1deg/INPUT -maxdepth 1 -type l    # prints only BROKEN links
```

## How it is wired on rancor

```
~/work/mmw/mom6sis2/.datasets -> /data/mom6-datasets
```

`/data/mom6-datasets` is assembled, not downloaded wholesale:

- symlinks to the already-unpacked trees in `/data/OLD/MOM6_static`
  (`OM4_025`, `OM4_05`, `OM4_360x320_C180`, `CORE`, `GOLD_SIS`, `AM2_LM3_MOM6i_1deg`,
  `Baltic_OM4_025`, `obs`, `misc`)
- real directories for the pieces that tree lacks: `OM_1deg/`, `reanalysis/`

**This depends on `/data/OLD/MOM6_static` surviving.** If `/data/OLD` is ever purged, either
copy those subtrees into `/data/mom6-datasets` first or re-download the tarballs below.

## Upstream source

GFDL serves the tarballs over anonymous FTP:

```bash
curl ftp://ftp.gfdl.noaa.gov/perm/Alistair.Adcroft/MOM6-testing/          # long listing
curl -l ftp://ftp.gfdl.noaa.gov/perm/Alistair.Adcroft/MOM6-testing/       # names only
curl -O ftp://ftp.gfdl.noaa.gov/perm/Alistair.Adcroft/MOM6-testing/OM_1deg.tgz
```

Each tarball unpacks to a single top-level directory matching its name, so unpack directly
in the `.datasets` root. Sizes below are approximate and only meant for planning a
download; list the directory for the real ones.

| Tarball | ~Size | Contents / why |
|---|---|---|
| `OM_1deg.tgz` | 9 MB | `OM_1deg/INPUT` grid, topography, vgrid. Our development config. **Present.** |
| `reanalysis-sample.tgz` | 174 MB | `reanalysis/JRA55-do/v1.4.0/{short_sample,padded}`, the `JRA_*.nc` forcing `OM_1deg` links. **Present.** |
| `reanalysis.tgz` | 12.4 GB | Full JRA55-do v1.4.0. Only needed for multi-year JRA-forced runs. |
| `OM4_025.tgz` | 421 MB | Quarter degree. Newer than the `/data/OLD` copy; see the gap noted below. |
| `OM4_025.tgz.old` | 324 MB | The older quarter-degree pack, which is what `/data/OLD/MOM6_static/OM4_025` matches. |
| `OM4_05.tgz` | 89 MB | Half degree. |
| `obs.woa13.tgz` | 1.0 GB | `obs/NOAA-NODC/WOA13/v2a` monthly T/S used to initialize `OM_1deg`. **Present** via `/data/OLD`. |
| `obs.tgz` | 84 MB | Older WOA05 observational climatology. |
| `global.tgz` | 741 MB | Legacy `global/siena_201204` style datasets for old configs. |
| `CORE.tgz` | 895 MB | CORE/NYF v2 atmospheric forcing. |
| `Baltic_OM4_025.tgz`, `Baltic_OM4_05.tgz` | 1 MB, 0.2 MB | Small regional cases, handy smoke tests. |
| `GOLD_SIS.tgz`, `GOLD_SIS_025.tgz` | 1.0 GB, 129 MB | Legacy GOLD/SIS1 configs. |
| `MESO_025_23L.tgz`, `MESO_025_63L.tgz` | 152 MB, 601 MB | MESO idealized configs. |
| `AM2_LM3_MOM6i_1deg.tgz` | 3.4 GB | Fully coupled 1deg, includes atmosphere/land restarts. |
| `OM4_360x320_C180.tgz` | 1.1 GB | Coupled 1deg ocean on C180 atmosphere. |
| `CM2G63L.tgz` | 7.9 GB | CM2G coupled. |
| `MOM6_SIS_icebergs.tgz` | 0.1 MB | Iceberg test case. |
| `src_AM2_LM3_SIS1.tgz` | 27 MB | Source for the GFDL-only coupled components. |

There are also two loose directories on the FTP that are not tarballs:

- `OM4_025/` and `OM4_05/` hold individually-updated files (`ocean_hgrid.nc`,
  `ocean_topog.nc`, `ocean_mask.nc`, `ocean_static.nc`, plus `basin_codes` and a WOA05
  annual T/S for OM4_025). Use these to refresh single files without re-pulling a tarball.
- `hashed-files/` holds md5-named blobs referenced by the `hash.md5` manifests inside the
  unpacked trees.

The wiki page that documents all of this is
<https://github.com/NOAA-GFDL/MOM6-examples/wiki/Getting-started>. On GFDL machines the
link points at a shared copy instead (`/archive/gold/datasets` on PAN,
`/gpfs/f5/gfdl_o/world-shared/datasets` on Gaea).

### Known gap

`ice_ocean_SIS2/OM4_025/INPUT/mask_table.*` links are broken, because they live in the 2019
`OM4_025.tgz` and our local unpacked copy is from the older pack. Mask tables only matter if
a layout uses `MASKTABLE` to drop all-land processors. To fix, replace the
`/data/mom6-datasets/OM4_025` symlink with a real directory and unpack the current
`OM4_025.tgz` into it.

## SOCA-era data already on this machine

Separate from the GFDL datasets, `/data/OLD` holds the data the previous SOCA experiments
ran on. Nothing here is re-downloadable; treat it as the only copy.

| Path | What |
|---|---|
| `/data/OLD/work_old/mom6sis2_global/{1deg,05deg,025deg,common}/{model,soca}` | The `mom6sis2_global.tgz` that the soca-science README points at via a dead Google Drive link. Per-resolution MOM6 `INPUT` plus SOCA's `godas_sst_bgerr.nc` and `rossrad.dat`. |
| `/data/OLD/achto/FIX/{1deg,05deg,025deg,common}` | A second variant of the same. Has `chl.nc`, `sgs_h2.nc`, `tideamp.nc`; lacks `KH_background_2d.nc`, `seawifs_*`, `topo_edits_*`. `FIX/FORC/` has GFS surface forcing fields. |
| `/data/OLD/soca_science_expdata/DATA/mom6sis2_global.360x320x75/model_input/` | 1deg model input plus the SOCA aux files, in the layout the old workflow's `MODEL_DATA_DIR` expects. |
| `/data/OLD/soca_science_expdata/DATA/mom6sis2_global.360x320x75/rst/` | MOM6 + SIS2 + icebergs restarts at two dates (2014-01-01T12, 2015-01-01T12). A ready-made spun-up initial condition, avoids a cold start. |
| `/data/OLD/soca_science_expdata/DATA/forcing/{era5,gfs}.1deg.6hr/` | 6-hourly surface forcing, ERA5 and GFS, zipped per year. |
| `/data/OLD/soca_science_expdata/DATA/obs/` | IODA observation files, ~9 GB. |
| `~/work/soca-science-v3.test/model_data/mom6sis2_1deg/` | 1deg `INPUT` including an `ic.nc` cold-start file, wired the way socasci v3 expected (`RESTART_IN` symlink). |

`/data/OLD/{jedi-soca,achto,hgodas_sst,marine_realtime}` hold more of the same era and have
not been fully mined.
