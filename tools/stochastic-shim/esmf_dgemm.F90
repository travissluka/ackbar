!> `esmf_dgemm`, for a build that has BLAS but not ESMF.
!!
!! `spectral_transforms.F90` in NOAA-PSL/stochastic_physics calls `dgemm`
!! under `-DCESMCOUPLED` and `esmf_dgemm` otherwise, and `esmf_dgemm` is
!! ESMF's own copy of the BLAS routine, with the same arguments. MOM6-SIS2
!! links neither ESMF nor, ordinarily, BLAS.
!!
!! Defining the missing name over the real BLAS is preferred to compiling the
!! generator with `-DCESMCOUPLED`, because that macro is global to the build
!! and MOM6 and SIS2 are compiled from the same command line. One added symbol
!! cannot reach anything that does not ask for it by name; one added macro can.
!!
!! `double precision` rather than `real(kind=8)` because the build carries
!! `-fdefault-real-8 -fdefault-double-8`, under which both are eight bytes.
!! Written as `real` it would silently become the same thing today and stop
!! matching the moment either flag moved.
subroutine esmf_dgemm(transa, transb, m, n, k, alpha, a, lda, b, ldb, beta, c, ldc)
  implicit none
  character,        intent(in)    :: transa, transb
  integer,          intent(in)    :: m, n, k, lda, ldb, ldc
  double precision, intent(in)    :: alpha, beta
  double precision, intent(in)    :: a(lda, *), b(ldb, *)
  double precision, intent(inout) :: c(ldc, *)
  external :: dgemm

  call dgemm(transa, transb, m, n, k, alpha, a, lda, b, ldb, beta, c, ldc)
end subroutine esmf_dgemm
