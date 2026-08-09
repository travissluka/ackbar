#!/bin/bash
# Pull the GEFSv12 reforecast fields needed to force MOM6-SIS2, one directory
# per member.
#
#   tools/spike-gefs-fetch.sh 2015071200 /data/ackbar/spike/gefs
#
# GEFSv12 *reforecast* rather than the operational GEFS archive that
# soca-science' `scripts/forc/forc_gefs.py` reads. That bucket
# (`noaa-gefs-pds`) starts in 2017 and this OSSE is 2015, so the operational
# archive cannot force it at all. The reforecast covers 2000-2019 with five
# members (c00 plus p01-p04) initialised at 00Z, each carrying ten days, which
# means one initialisation supplies every member and every lead this spike
# needs.
#
# Five members is a real constraint, not a choice: it is what the reforecast
# has. An ensemble larger than five wants either the operational archive and a
# later year, or ERA5's ensemble of data assimilations.
set -euo pipefail

INIT=${1:?usage: spike-gefs-fetch.sh <YYYYMMDDHH> <out-dir>}
OUT=${2:?usage: spike-gefs-fetch.sh <YYYYMMDDHH> <out-dir>}
YEAR=${INIT:0:4}

BUCKET=s3://noaa-gefs-retrospective/GEFSv12/reforecast

# The seven that the bulk formulae and the radiation need. `pres_msl` is absent
# because the data table defaults surface pressure to a constant, and adding a
# real one is a change to the forcing everything reads rather than to this
# spike. Wind is `*_hgt`, not `*_10m`: the reforecast files the 10 m winds under
# a height level.
FIELDS=(tmp_2m spfh_2m ugrd_hgt vgrd_hgt dswrf_sfc dlwrf_sfc apcp_sfc)
MEMBERS=(c00 p01 p02 p03 p04)

mkdir -p "$OUT"
for member in "${MEMBERS[@]}"; do
    mkdir -p "$OUT/$member"
    for field in "${FIELDS[@]}"; do
        target=$OUT/$member/$field.grib2
        # Size, not existence: an interrupted `aws s3 cp` leaves a short file
        # that exists, and eccodes reads it as a truncated message rather than
        # as a missing one.
        if [[ -s $target ]]; then
            echo "have $member/$field"
            continue
        fi
        source=$BUCKET/$YEAR/$INIT/$member/Days:1-10/${field}_${INIT}_${member}.grib2
        echo "get  $member/$field"
        aws s3 cp --no-sign-request --only-show-errors "$source" "$target.part"
        mv "$target.part" "$target"
    done
done

echo "spike-gefs-fetch: $OUT"
du -sh "$OUT"
