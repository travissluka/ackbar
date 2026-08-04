# (C) Copyright 2026 UCAR
#
# The ACKBAR JEDI bundle: SOCA and the repositories it actually needs.
#
# Trimmed from the Skylab bundle. Everything for other models (fv3-jedi,
# mpas-jedi, coupling, pyiri-jedi), every observation operator ocean DA does not
# use (rttov, ropp, gsibec), and every test-data repository is gone. What is
# left is SOCA's dependency closure and nothing else.
#
# Sources are git submodules of the ackbar repository, so the GIT lines here
# carry no BRANCH or UPDATE: ecbuild leaves an existing directory alone, and
# git owns which commit is checked out. Update with
#   git -C <repo> checkout <ref> && git -C ~/work/ackbar add pkg/jedi/<repo>
# not by letting the build system move the source underneath you.
#
# eckit, fckit, atlas, fms and jedi-cmake come from spack-stack, not from here.

cmake_minimum_required( VERSION 3.14 FATAL_ERROR )

find_package( ecbuild 3.6 REQUIRED HINTS ${CMAKE_CURRENT_SOURCE_DIR} ${CMAKE_CURRENT_SOURCE_DIR}/../ecbuild )

project( ackbar-jedi VERSION 1.0.0 LANGUAGES C CXX Fortran )

list( APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake" )

include( ecbuild_bundle )

# Default release mode
set( ECBUILD_DEFAULT_BUILD_TYPE Release )

# Enable OpenMP and MPI
set( ENABLE_MPI ON CACHE BOOL "Compile with MPI" )
set( ENABLE_OMP ON CACHE BOOL "Compile with OpenMP" )

# Tests are ON by default: SOCA's own ctests are self contained and are the way
# to tell a build is sane.
#
# The knob is CMake's own BUILD_TESTING, set by include(CTest). ecbuild has no
# ENABLE_TESTS of its own, and IODA and UFO branch directly on BUILD_TESTING.
#
# Leaving it on costs a one time download of the IODA and UFO test tarballs from
# an external host, because the test-data repositories (ioda-data, ufo-data,
# jedi-model-data) are deliberately not in this bundle. Configure with
# -DBUILD_TESTING=OFF to skip both the download and every ctest.
option( BUILD_TESTING "Build ctests" ON )

# Stop search as soon as Python satisfying version constraints is found.
set( Python3_FIND_STRATEGY LOCATION )

# Depend path for non-ecbuild packages
set( DEPEND_LIB_ROOT ${CMAKE_CURRENT_BINARY_DIR}/Depends )
list( APPEND CMAKE_PREFIX_PATH ${DEPEND_LIB_ROOT} )

# Library path for non-ecbuild packages
link_directories( ${CMAKE_CURRENT_BINARY_DIR}/lib )

include( GNUInstallDirs )
if( APPLE )
  list( APPEND CMAKE_INSTALL_RPATH $ENV{llvm_openmp_ROOT}/lib )
endif()

# add the automatically determined parts of the RPATH
# which point to directories outside the build tree to the install RPATH
set( CMAKE_INSTALL_RPATH_USE_LINK_PATH ON )

# when building, already use the install RPATH
set( CMAKE_BUILD_WITH_INSTALL_RPATH ON )

# Define bundle
ecbuild_bundle_initialize()

# Use external jedi-cmake
include( $ENV{jedi_cmake_ROOT}/share/jedicmake/Functions/git_functions.cmake )

# --- SOCA's dependency closure -----------------------------------------------
# GSW-Fortran: TEOS-10. UFO's in situ temperature and salinity operators use it,
# so it is required here even though UFO itself find_packages it QUIET.
ecbuild_bundle( PROJECT gsw      GIT "ssh://git@github.com/jcsda-internal/GSW-Fortran.git" )

ecbuild_bundle( PROJECT oops     GIT "ssh://git@github.com/jcsda-internal/oops.git" )
ecbuild_bundle( PROJECT vader    GIT "ssh://git@github.com/jcsda-internal/vader.git" )
ecbuild_bundle( PROJECT saber    GIT "ssh://git@github.com/jcsda-internal/saber.git" )
ecbuild_bundle( PROJECT ioda     GIT "ssh://git@github.com/jcsda-internal/ioda.git" )
ecbuild_bundle( PROJECT ufo      GIT "ssh://git@github.com/jcsda-internal/ufo.git" )

# SOCA carries its own nested submodules (NOAA-EMC/MOM6, CICE-Consortium/Icepack).
# They are nested submodules of ackbar; init with
#   git submodule update --init --recursive pkg/jedi/soca
ecbuild_bundle( PROJECT soca     GIT "ssh://git@github.com/jcsda-internal/soca.git" )

# --- Optional, off by default ------------------------------------------------
# CRTM: radiance observation operators. Ocean DA assimilates retrieved SST
# products rather than radiances, so this is off. Turning it on adds the CRTM
# operator to UFO and a large build.
option( BUILD_CRTM "Build CRTM and the CRTM observation operator" OFF )
if( BUILD_CRTM )
  ecbuild_bundle( PROJECT crtm   GIT "https://github.com/jcsda/CRTMv3.git" BRANCH develop UPDATE )
endif()

# OASIM: ocean optics. Needed only for biogeochemistry and ocean colour.
option( BUILD_OASIM "Build OASIM ocean optics" OFF )
if( BUILD_OASIM )
  ecbuild_bundle( PROJECT oasim  GIT "ssh://git@github.com/jcsda-internal/oasim.git" BRANCH develop UPDATE )
endif()

# ioda-converters: not used in the cycle. ACKBAR builds observation archives
# offline (see docs/design.md, Observations), which is when this is wanted.
option( BUILD_IODA_CONVERTERS "Build IODA converters" OFF )
if( BUILD_IODA_CONVERTERS )
  ecbuild_bundle( PROJECT iodaconv GIT "ssh://git@github.com/jcsda-internal/ioda-converters.git" BRANCH develop UPDATE )
endif()

ecbuild_bundle_finalize()
