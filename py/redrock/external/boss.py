"""
redrock.external.boss
=====================

redrock wrapper tools for BOSS
"""
import os
import sys
import re
import warnings
import traceback

import argparse

import numpy as np
from scipy import sparse
from scipy.interpolate import interp1d, make_lsq_spline

from astropy.io import fits
from astropy.table import Table

import fitsio

import desispec.resolution
from desispec.resolution import Resolution

from ..utils import elapsed, get_mp, distribute_work

from ..targets import Spectrum, Target, DistTargetsCopy

from ..templates import load_dist_templates

from ..results import write_zscan

from ..zfind import zfind

from .._version import __version__

from ..archetypes import All_archetypes

from ..rebin import trapz_rebin, centers2edges


def platemjdfiber2targetid(plate, mjd, fiber):
    return plate*1000000000 + mjd*10000 + fiber


def targetid2platemjdfiber(targetid):
    fiber = targetid % 10000
    mjd = (targetid // 10000) % 100000
    plate = (targetid // (10000 * 100000))
    return (plate, mjd, fiber)


def write_zbest(outfile, zbest, template_version, archetype_version):
    """Write zbest Table to outfile

    Args:
        outfile (str): output file.
        zbest (Table): the output best fit results.

    """
    header = fits.Header()
    header['RRVER'] = (__version__, 'Redrock version')
    for i, fulltype in enumerate(template_version.keys()):
        header['TEMNAM'+str(i).zfill(2)] = fulltype
        header['TEMVER'+str(i).zfill(2)] = template_version[fulltype]
    if not archetype_version is None:
        for i, fulltype in enumerate(archetype_version.keys()):
            header['ARCNAM'+str(i).zfill(2)] = fulltype
            header['ARCVER'+str(i).zfill(2)] = archetype_version[fulltype]
    zbest.meta['EXTNAME'] = 'ZBEST'

    hx = fits.HDUList()
    hx.append(fits.PrimaryHDU(header=header))
    hx.append(fits.convenience.table_to_hdu(zbest))
    hx.writeto(os.path.expandvars(outfile), overwrite=True)
    return

### @profile
def read_spectra(spplates_name, targetids=None, use_frames=False,
    fiberid=None, coadd=False, cache_Rcsr=False, use_andmask=False,
    use_best_exp=False, use_random_exp=False, random_seed=0,
    coadd_frames=False,coadd_frames_interp='ngp'):
    """Read targets from a list of spectra files

    Args:
        spplates_name (list or str): input spPlate files or pattern to match files.
        targetids (list): restrict targets to this subset.
        use_frames (bool): if True, use frames.
        fiberid (int): Use this fiber ID.
        coadd (bool): if True, compute and use the coadds.
        cache_Rcsr (bool): pre-calculate and cache sparse CSR format of
            resolution matrix R
        use_andmask (bool): sets ivar = 0 to pixels with and_mask != 0

    Returns:
        tuple: (targets, meta) where targets is a list of Target objects and
        meta is a Table of metadata (currently only BRICKNAME).

    """

    # check the file list
    if isinstance(spplates_name, str):
        import glob
        spplates_name = glob.glob(spplates_name)

    ## read spplates
    useThingid = False
    if len(spplates_name)>1:
        print("DEBUG: Reading multiple observations: using THING_ID instead of PLATE*1000000000 + MJD*10000 + FIBERID")
        useThingid = True
        fiberid2thingid = {}
    plate = []
    mjd = []
    infiles = []
    for spplate_name in spplates_name:

        spplate = fitsio.FITS(spplate_name)
        plate += [spplate[0].read_header()["PLATEID"]]
        mjd += [spplate[0].read_header()["MJD"]]

        if useThingid:
            photoPlate = fitsio.FITS(spplate_name.replace('spPlate','photoPosPlate'))

        cameras = ['b1','r1','b2','r2']
        if use_frames:
            path = os.path.dirname(spplate_name)

            nexp_tot=0
            for c in cameras:
                try:
                    nexp = spplate[0].read_header()["NEXP_{}".format(c.upper())]
                except ValueError:
                    print("DEBUG: spplate {} has no exposures in camera {} ".format(spplate_name,c))
                    continue
                for i in range(1,nexp+1):
                    nexp_tot += 1
                    expid = str(nexp_tot).zfill(2)
                    exp = path+"/spCFrame-"+spplate[0].read_header()["EXPID"+expid][:11]+".fits"
                    infiles.append(exp)
                    if useThingid:
                        fiberid2thingid[exp] = photoPlate[1]['THING_ID'][:]
        elif use_best_exp:
            path = os.path.dirname(spplate_name)

            bestexp = spplate[0].read_header()["BESTEXP"]
            expid = str(bestexp).zfill(8)
            for c in cameras:
                exp = path+"/spCFrame-"+c+'-'+expid+".fits"
                infiles.append(exp)

                ## Unsure about this: copied from use_frames.
                if useThingid:
                    fiberid2thingid[exp] = photoPlate[1]['THING_ID'][:]
            print("DEBUG: using best exposure",expid)
        elif use_random_exp:
            path = os.path.dirname(spplate_name)

            ## For each exposure that went into the spplate file, extract the
            ## expid from the spplate header. Remove duplicates (from different
            ## cameras) and put into a random order. Use [plate,mjd,random_seed]
            ## as a random seed.
            nexp = spplate[0].read_header()["NEXP"]
            expids = list(set([spplate[0].read_header()["EXPID"+str(n+1).zfill(2)][3:11] for n in range(nexp)]))
            expids.sort()
            gen = np.random.RandomState(seed=[spplate[0].read_header()["PLATEID"],spplate[0].read_header()["MJD"],random_seed])
            gen.shuffle(expids)

            ## For each expid:
            ind = 0
            exit = False
            while (ind<len(expids)) and (not exit):
                expid = expids[ind]
                ind += 1

                # Check that this exposure exists for all cameras.
                files_exist = True
                for c in cameras:
                    exp = path+"/spCFrame-"+c+'-'+expid+".fits"
                    if not os.path.isfile(exp):
                        files_exist &= False

                # If so, add exposures to the list of infiles.
                if files_exist:
                    for c in cameras:
                        exp = path+"/spCFrame-"+c+'-'+expid+".fits"
                        infiles.append(exp)

                        ## Unsure about this: copied from use_frames.
                        if useThingid:
                            fiberid2thingid[exp] = photoPlate[1]['THING_ID'][:]
                    # Exit the while loop.
                    exit = True

            # If we did not find files, print a notification.
            if not files_exist:
                print("DEBUG: could not find spCFrame files for all cameras for any single exposure in spplate {}".format(spplate_name))
                continue
            else:
                print("DEBUG: using randomly chosen exposure",expid)
        else:
            infiles.append(spplate_name)
            if useThingid:
                fiberid2thingid[spplate_name] = photoPlate[1]['THING_ID'][:]

        spplate.close()
        if useThingid:
            photoPlate.close()

    if len(set(plate))==1:
        plate = plate[0]
    else:
        plate = 0
    if len(set(mjd))==1:
        mjd = mjd[0]
    else:
        mjd = 0

    #print('in files has {} entries'.format(len(infiles)))
    #print('first entry is:')
    #print(infiles[0])
    #sys.stdout.flush()

    bricknames={}
    dic_spectra = {}

    for infile in infiles:
        h = fitsio.FITS(infile)
        if not useThingid:
            assert plate == h[0].read_header()["PLATEID"]
        fs = h[5]["FIBERID"][:]
        if fiberid is not None:
            w = np.in1d(fs,fiberid)
            fs = fs[w]

        fl = h[0].read()
        iv = h[1].read()
        if use_andmask:
            iv *= 1.*(h[2].read()==0)
        wd = h[4].read()

        ## crop to lmin, lmax
        lmin = 3500.
        lmax = 10000.
        if use_frames or use_best_exp or use_random_exp:
            # Get the lambda grid from the spcframe file.
            la = 10**h[3].read()
            #print('wavelength grid has shape',la.shape,'before hack')

            if coadd_frames:
                # Find the spplate file that the spcframe went into.
                path = os.path.dirname(infile)
                f_spplate = path+'/spPlate-{}-{}.fits'.format(plate,mjd)
                spplate = fitsio.FITS(f_spplate)

                # Get its wavelength grid.
                coeff0 = spplate[0].read_header()["COEFF0"]
                coeff1 = spplate[0].read_header()["COEFF1"]
                fl_spplate = spplate[0].read()
                la_spplate = 10**(coeff0 + coeff1*np.arange(fl_spplate.shape[1]))
                la_spplate_edges = centers2edges(la_spplate)

                # Rebin each spectrum onto the spplate file's wavelength grid.
                # This is done in a *very* basic way at the moment.
                nspec = fl.shape[0]
                npix = la_spplate.shape[0]
                fl_new = np.zeros((nspec,npix))
                iv_new = np.zeros((nspec,npix))
                wd_new = np.zeros((nspec,npix))
                for i in range(nspec):
                    if coadd_frames_interp=='trapz':
                        ## Integration method.
                        w = (la_spplate_edges[:-1]>la[i,0]) & (la_spplate_edges[1:]<la[i,-1])
                        w_edges = (la_spplate_edges>la[i,0]) & (la_spplate_edges<la[i,-1])
                        fl_new[i,w] = trapz_rebin(la[i,:], fl[i,:], edges=la_spplate_edges[w_edges])
                        iv_new[i,w] = trapz_rebin(la[i,:], iv[i,:], edges=la_spplate_edges[w_edges])
                        wd_new[i,w] = trapz_rebin(la[i,:], wd[i,:], edges=la_spplate_edges[w_edges])

                    elif coadd_frames_interp=='ngp':
                        ## NGP method.
                        fl_new[i,:] = interp1d(la[i,:], fl[i,:], kind='nearest', fill_value=0., bounds_error=False)(la_spplate)
                        iv_new[i,:] = interp1d(la[i,:], iv[i,:], kind='nearest', fill_value=0., bounds_error=False)(la_spplate)
                        wd_new[i,:] = interp1d(la[i,:], wd[i,:], kind='nearest', fill_value=0., bounds_error=False)(la_spplate)

                    elif coadd_frames_interp=='spline':
                        ## Pipeline method: spline for flux, and linear for iv and disp.
                        ## Note: I haven't included the iterative pixel rejection/spline building here.

                        # Do spline interpolation.
                        zero_old_iv = (iv[i,:]==0)
                        fl_new[i,:] = interp1d(la[i,~zero_old_iv], fl[i,~zero_old_iv], kind='cubic', fill_value=0., bounds_error=False)(la_spplate)

                        # Do linear interpolation on iv, setting the value to zero in any new pixels that have contributions
                        # from any old pixels with iv=0
                        iv_new[i,:] = interp1d(la[i,:], iv[i,:], kind='linear', fill_value=0., bounds_error=False)(la_spplate)
                        new_pix_w_zero_old_iv = (interp1d(la[i,:], w, kind='linear', fill_value=0., bounds_error=False)(la_spplate))>0
                        iv_new[new_pix_w_zero_old_iv] = 0.

                        # Do linear interpolation for dispersion.
                        wd_new[i,:] = interp1d(la[i,:], wd[i,:], kind='linear', fill_value=0., bounds_error=False)(la_spplate)

                    elif coadd_frames_interp=='spline_simple':
                        ## Simple spline.
                        fl_new[i,:] = interp1d(la[i,:], fl[i,:], kind='cubic', fill_value=0., bounds_error=False)(la_spplate)
                        iv_new[i,:] = interp1d(la[i,:], iv[i,:], kind='linear', fill_value=0., bounds_error=False)(la_spplate)
                        wd_new[i,:] = interp1d(la[i,:], wd[i,:], kind='linear', fill_value=0., bounds_error=False)(la_spplate)


                # Overwrite the data from the spcframe file.
                fl = fl_new
                iv = iv_new
                wd = wd_new
                la = np.broadcast_to(la_spplate,fl.shape)

            else:
                if h[0].read_header()["CAMERAS"][0]=="b":
                    lmax = 6000.
                else:
                    lmin = 5500.

        else:
            coeff0 = h[0].read_header()["COEFF0"]
            coeff1 = h[0].read_header()["COEFF1"]
            la = 10**(coeff0 + coeff1*np.arange(fl.shape[1]))
            la = np.broadcast_to(la,fl.shape)

        #print('wavelength grid has shape',la.shape,'after hack')
        #sys.stdout.flush()

        h.close()

        imin = abs(la-lmin).min(axis=0).argmin()
        imax = abs(la-lmax).min(axis=0).argmin()

        la = la[:,imin:imax]
        fl = fl[:,imin:imax]
        iv = iv[:,imin:imax]
        wd = wd[:,imin:imax]

        w = wd<1e-5
        wd[w]=2.
        ii = np.arange(la.shape[1])
        di = ii-ii[:,None]
        di2 = di**2
        ndiag = int(4*np.ceil(wd.max())+1)
        nbins = wd.shape[1]

        for f in fs:
            i = (f-1)
            if use_frames or use_best_exp or use_random_exp:
                i = i%500

            if useThingid:
                t = fiberid2thingid[infile][f-1]
            else:
                t = platemjdfiber2targetid(plate, mjd, f)
            if not targetids is None and not t in targetids:
                continue

            if t not in dic_spectra:
                dic_spectra[t]=[]
                brickname = '{}-{}'.format(plate,mjd)
                bricknames[t] = brickname

            ## build resolution from wdisp
            reso = np.zeros([ndiag,nbins])
            for idiag in range(ndiag):
                offset = ndiag//2-idiag
                d = np.diagonal(di2,offset=offset)
                if offset<0:
                    reso[idiag,:len(d)] = np.exp(-d/2/wd[i,:len(d)]**2)
                else:
                    reso[idiag,nbins-len(d):nbins]=np.exp(-d/2/wd[i,nbins-len(d):nbins]**2)

            # R = Resolution(reso)
            # ccd = sparse.spdiags(1./R.sum(axis=1).T, 0, *R.shape)
            # R = (ccd*R).todia()

            reso /= np.sum(reso, axis=0)
            offsets = ndiag//2 - np.arange(ndiag)
            nwave = reso.shape[1]
            R = sparse.dia_matrix((reso, offsets), (nwave, nwave))

            if cache_Rcsr:
                Rcsr = R.tocsr()
            else:
                Rcsr = None

            dic_spectra[t].append(Spectrum(la[i], fl[i], iv[i], R, Rcsr))

        #h.close()
        print("DEBUG: read {} ".format(infile))
        sys.stdout.flush()

    if targetids == None:
        targetids = sorted(list(dic_spectra.keys()))
    else:
        targetids = sorted(targetids)

    targets = []
    for targetid in targetids:
        spectra = dic_spectra[targetid]
        # Add the brickname to the meta dictionary.  The keys of this dictionary
        # will end up as extra columns in the output ZBEST HDU.
        tmeta = dict()
        tmeta["BRICKNAME"] = bricknames[targetid]
        tmeta["BRICKNAME_datatype"] = "S8"
        if len(spectra) > 0:
            targets.append(Target(targetid, spectra, coadd=coadd, meta=tmeta))
            #if targetid==targetids[0]:
                #print('first targetid =',targetid)
                #print('has {} spectra going in'.format(len(spectra)))
                #print('these have first elements in waves:')
                #print([s.wave[0] for s in spectra])
                #print('these have wavehashes:')
                #print([s.wavehash for s in spectra])
                #print('spectra will be coadded?',coadd)
                #print('has {} spectra in Target object'.format(len(Target(targetid, spectra, coadd=coadd, meta=tmeta).spectra)))
                #print(' ')
                #sys.stdout.flush()
        else:
            print('ERROR: Target {} on {} has no good spectra'.format(targetid, os.path.basename(brickfiles[0])))

    #print('list of targets made')
    #sys.stdout.flush()

    #- Create a metadata table in case we might want to add other columns
    #- in the future
    for k in sorted(list(bricknames.keys())):
        if k not in targetids:
            del bricknames[k]
    assert len(bricknames.keys()) == len(targets)

    metatable = Table()
    metatable['TARGETID'] = targetids
    bx = np.array([bricknames[t] for t in targetids], dtype='S8')
    metatable['BRICKNAME'] = bx

    # metatable = Table(names=("TARGETID", "BRICKNAME"), dtype=("i8", "S8",))
    # for i, t in enumerate(targetids):
    #     metatable.add_row( (t, bricknames[t]) )

    return targets, metatable


def rrboss(options=None, comm=None):
    """Estimate redshifts for BOSS targets.

    This loads targets serially and copies them into a DistTargets class.
    It then runs redshift fitting and writes the output to a catalog.

    Args:
        options (list): optional list of commandline options to parse.
        comm (mpi4py.Comm): MPI communicator to use.

    """
    global_start = elapsed(None, "", comm=comm)

    parser = argparse.ArgumentParser(description="Estimate redshifts from"
        " BOSS target spectra.")

    parser.add_argument("--spplate", type=str, default=None,
        required=True, help="input plate files", nargs='*')

    parser.add_argument("-t", "--templates", type=str, default=None,
        required=False, help="template file or directory")

    parser.add_argument("--archetypes", type=str, default=None,
        required=False, help="archetype file or directory for final redshift comparisons")

    parser.add_argument("-o", "--output", type=str, default=None,
        required=False, help="output file")

    parser.add_argument("--zbest", type=str, default=None,
        required=False, help="output zbest FITS file")

    parser.add_argument("--targetids", type=str, default=None,
        required=False, help="comma-separated list of target IDs")

    parser.add_argument("--mintarget", type=int, default=None,
        required=False, help="first target to process")

    parser.add_argument("--priors", type=str, default=None,
        required=False, help="optional redshift prior file")

    parser.add_argument("--chi2-scan", type=str, default=None,
        required=False, help="Load the chi2-scan from the input file")

    parser.add_argument("-n", "--ntargets", type=int,
        required=False, help="the number of targets to process")

    parser.add_argument("--nminima", type=int, default=3,
        required=False, help="the number of redshift minima to search")

    parser.add_argument("--allspec", default=False, action="store_true",
        required=False, help="use individual spectra instead of coadd")

    parser.add_argument("--mp", type=int, default=0,
        required=False, help="if not using MPI, the number of multiprocessing"
            " processes to use (defaults to half of the hardware threads)")

    parser.add_argument("--use-frames", default=False, action="store_true",
        required=False, help="use individual spcframes instead of spplate "
        "(the spCFrame files are expected to be in the same directory as "
        "the spPlate")

    parser.add_argument("--use-andmask", default=False, action="store_true",
        required=False, help="uses and_mask values to set masked pixel's ivar to zero")

    parser.add_argument("--no-mpi-abort", default=False, action="store_true",
        required=False, help="Do not call MPI Abort upon failure of a single rank")

    parser.add_argument("--debug", default=False, action="store_true",
        required=False, help="debug with ipython (only if communicator has a "
        "single process)")

    parser.add_argument("--use-best-exp", default=False, action="store_true",
        required=False, help="use individual spcframe files from the best exp  "
        "instead of spplate (the spCFrame files are expected to be in the same "
        "directory as the spPlate)")

    parser.add_argument("--use-random-exp", default=False, action="store_true",
        required=False, help="use individual spcframe files from a random exp  "
        "instead of spplate (the spCFrame files are expected to be in the same "
        "directory as the spPlate)")

    parser.add_argument("--coadd-frames", default=False, action="store_true",
        required=False, help="if using spcframe files, coadd spectra across "
        "the blue and red cameras.")

    parser.add_argument("--coadd-frames-interp", default=False, action="store_true",
        choices = ['trapz','ngp','spline','spline_simple']
        required=False, help="temporary option to control interpolation method "
        "when coadding frames.")

    parser.add_argument("--random-seed", type=int, default=0,
        required=False, help="seed for choosing random exposure")

    args = None
    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)

    comm_size = 1
    comm_rank = 0
    if comm is not None:
        comm_size = comm.size
        comm_rank = comm.rank

    # Check arguments- all processes have this, so just check on the first
    # process

    if comm_rank == 0:
        if args.debug and comm_size != 1:
            print("--debug can only be used if the communicator has one "
                " process")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()

        if (args.output is None) and (args.zbest is None):
            print("--output or --zbest required")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()

        if (args.targetids is not None) and ((args.mintarget is not None) \
            or (args.ntargets is not None)):
            print("cannot select targets by both ID and range")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()

    targetids = None
    if args.targetids is not None:
        targetids = [ int(x) for x in args.targetids.split(",") ]

    n_targets = None
    if args.ntargets is not None:
        n_targets = args.ntargets

    first_target = None
    if args.mintarget is not None:
        first_target = args.mintarget
    elif n_targets is not None:
        first_target = 0

    if args.allspec & args.coadd_frames:
        raise ValueError('Cannot use options allspec and coadd-frames simultaneously!')

    # Multiprocessing processes to use if MPI is disabled.
    mpprocs = 0
    if comm is None:
        mpprocs = get_mp(args.mp)
        print("Running with {} processes".format(mpprocs))
        if "OMP_NUM_THREADS" in os.environ:
            nthread = int(os.environ["OMP_NUM_THREADS"])
            if nthread != 1:
                print("WARNING:  {} multiprocesses running, each with "
                    "{} threads ({} total)".format(mpprocs, nthread,
                    mpprocs*nthread))
                print("WARNING:  Please ensure this is <= the number of "
                    "physical cores on the system")
        else:
            print("WARNING:  using multiprocessing, but the OMP_NUM_THREADS")
            print("WARNING:  environment variable is not set- your system may")
            print("WARNING:  be oversubscribed.")
        sys.stdout.flush()
    elif comm_rank == 0:
        print("Running with {} processes".format(comm_size))
        #print("pre flush")
        sys.stdout.flush()
        #print("flushed")

    try:
        # Load and distribute the targets
        if comm_rank == 0:
            print("Loading targets...")
            #print("pre flush")
            sys.stdout.flush()
            #print("flushed")

        start = elapsed(None, "", comm=comm)

        # Read the spectra on the root process.  Currently the "meta" Table
        # returned here is not propagated to the output zbest file.  However,
        # that could be changed to work like the DESI write_zbest() function.
        # Each target contains metadata which is propagated to the output zbest
        # table though.
        #print('checkpoint: start read_spectra')
        #sys.stdout.flush()
        targets, meta = read_spectra(args.spplate, targetids=targetids,
            use_frames=args.use_frames, coadd=(not args.allspec),
            cache_Rcsr=True, use_andmask=args.use_andmask,
            use_best_exp=args.use_best_exp, use_random_exp=args.use_random_exp,
            random_seed=args.random_seed, coadd_frames=args.coadd_frames,
            coadd_frames_interp=args.coadd_frames_interp)
        #print('checkpoint: end read_spectra')
        #print('{} targets read'.format(len(targets)))
        #sys.stdout.flush()

        if args.ntargets is not None:
            targets = targets[first_target:first_target+n_targets]
            meta = meta[first_target:first_target+n_targets]

        stop = elapsed(start, "Read of {} targets"\
            .format(len(targets)), comm=comm)

        # Distribute the targets.

        start = elapsed(None, "", comm=comm)

        dtargets = DistTargetsCopy(targets, comm=comm, root=0)

        # Get the dictionary of wavelength grids
        dwave = dtargets.wavegrids()

        #print('\n\n\nlen(dwave) =',len(dwave),'\n\n\n')
        #sys.stdout.flush()

        stop = elapsed(start, "Distribution of {} targets"\
            .format(len(dtargets.all_target_ids)), comm=comm)

        # Read the template data

        #print('checkpoint: start load_dist_templates')
        #sys.stdout.flush()
        dtemplates = load_dist_templates(dwave, templates=args.templates,
            comm=comm, mp_procs=mpprocs)
        #print('checkpoint: enc load_dist_templates')
        #sys.stdout.flush()

        # Compute the redshifts, including both the coarse scan and the
        # refinement.  This function only returns data on the rank 0 process.

        start = elapsed(None, "", comm=comm)

        scandata, zfit = zfind(dtargets, dtemplates, mpprocs,
            nminima=args.nminima, archetypes=args.archetypes,
            priors=args.priors, chi2_scan=args.chi2_scan)

        stop = elapsed(start, "Computing redshifts took", comm=comm)

        # Write the outputs

        if args.output is not None:
            start = elapsed(None, "", comm=comm)
            if comm_rank == 0:
                write_zscan(args.output, scandata, zfit, clobber=True)
            stop = elapsed(start, "Writing zscan data took", comm=comm)

        if args.zbest:
            start = elapsed(None, "", comm=comm)
            if comm_rank == 0:
                zbest = zfit[zfit['znum'] == 0]

                # Remove extra columns not needed for zbest
                zbest.remove_columns(['zz', 'zzchi2', 'znum'])

                # Change to upper case like DESI
                for colname in zbest.colnames:
                    if colname.islower():
                        zbest.rename_column(colname, colname.upper())

                template_version = {t._template.full_type:t._template._version for t in dtemplates}
                archetype_version = None
                if not args.archetypes is None:
                    archetypes = All_archetypes(archetypes_dir=args.archetypes).archetypes
                    archetype_version = {name:arch._version for name, arch in archetypes.items() }
                write_zbest(args.zbest, zbest, template_version, archetype_version)

            stop = elapsed(start, "Writing zbest data took", comm=comm)

    except Exception as err:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        lines = [ "Proc {}: {}".format(comm_rank, x) for x in lines ]
        print("--- Process {} raised an exception ---".format(comm_rank))
        print("".join(lines))
        sys.stdout.flush()
        if comm is None or args.no_mpi_abort:
            raise err
        else:
            comm.Abort()

    global_stop = elapsed(global_start, "Total run time", comm=comm)

    if args.debug:
        import IPython
        IPython.embed()
