# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-
"""
The Monte Carlo code that adds fake galaxiesto images from the Legacy Survey
"""

from __future__ import division, print_function

if __name__ == '__main__':
    import matplotlib
    matplotlib.use('Agg')
#import h5py
import os
import sys
import subprocess
import time as time_builtin
import shutil
import logging
import argparse
import photutils

import numpy as np
import matplotlib.pyplot as plt
from pkg_resources import resource_filename
from pickle import dump
from glob import glob
import csv

from astropy.table import Table, Column, vstack
from astropy.io import fits
#from astropy import wcs as astropy_wcs
from fitsio import FITSHDR
import fitsio

from astropy import units
from astropy.coordinates import SkyCoord

#obiwan
from db_tools import getSrcsInBrick
from common import get_outdir_runbrick, get_brickinfo_hack
from common import stack_tables

# Sphinx build would crash
#try:
from legacypipe.runbrick import run_brick
from legacypipe.decam import DecamImage
from legacypipe.bok import BokImage
from legacypipe.mosaic import MosaicImage
from legacypipe.survey import LegacySurveyData, wcs_for_brick
from legacypipe.runcosmos import DecamImagePlusNoise, CosmosSurvey

from astrometry.util.fits import fits_table, merge_tables
from astrometry.util.ttime import Time
from astrometry.libkd.spherematch import match_radec

from tractor.psfex import PsfEx, PsfExModel
from tractor.basics import GaussianMixtureEllipsePSF, RaDecPos
from tractor.sfd import SFDMap
from tractor.galaxy import DevGalaxy, ExpGalaxy
from legacypipe.survey import LegacyEllipseWithPriors
import tractor
from tractor import *



import galsim
#except ImportError:
#    pass

DATASETS=['cosmos','dr8','dr9']
BRICKNAME='BRICKNAME'
SURVEY_DIR='SURVEY_DIR'
NN=0
def write_dict(fn,d):
    '''d -- dictionary'''
    w = csv.writer(open(fn, "w"))
    for key, val in d.items():
        w.writerow([key, val])

def read_dict(fn):
    d = {}
    for key, val in csv.reader(open(fn)):
        d[key] = val
    return d

def imshow_stamp(stamp,fn='test.png',galsimobj=True):
    if galsimobj:
        img = stamp.array.copy()
    else:
        img= stamp.copy()
    img=img + abs(img.min())+1
    plt.imsave(fn,np.log10(img),origin='lower',cmap='gray')
    #plt.imshow(np.log10(img),origin='lower',cmap='gray')
    #plt.savefig(fn)
    #plt.close()
    #print('Wrote %s' % fn)

def plot_radial_profs(fn,profs):
    assert(profs.shape[1] == 3)
    r=np.arange(profs.shape[0])
    for i,lab in zip(range(3),['src','srcnoise','srcnoiseimg']):
        plt.plot(r,profs[:,i],label=lab)
    plt.legend(loc='lower right')
    plt.savefig(fn)
    plt.close()


def ptime(text,t0):
    '''Timer'''
    tnow=Time()
    print('TIMING:%s ' % text,tnow-t0)
    return tnow


def get_skip_ids(decals_sim_dir, brickname, objtype):
    fns= glob(os.path.join(decals_sim_dir, objtype,
                         brickname[:3], brickname,
                         '*','obiwan','skippedids-*.fits'))
    if len(fns) == 0:
        raise ValueError("no skippedids.fits files exist for this brick %s" % brickname)
    T= stack_tables(fns, textfile=False)
    return T.ids.astype(str)

def get_fnsuffix(**kwargs):
    return '-{}-{}.fits'.format(kwargs['objtype'], kwargs['brickname'])
                                   #'rs%d' % kwargs['rowst'])

# try:
class SimDecals(LegacySurveyData):
    """Top level object that specifying which data to run through pipeline

    Same behavior as legacypipe.runs.Dr3DecalsSurvey which chooses which
        CCDs to include. But this also stores all the relevant obiwan
        objects

    Args:
        dataset: see definition in
            https://github.com/legacysurvey/obiwan/blob/master/py/obiwan/test/end_to_end/README.md
        survey_dir: as used by legacypipe.runbrick.run_brick()
            Defaults to $LEGACY_SURVEY_DIR environment variable.  Where to look for
            files including calibration files, tables of CCDs and bricks, image data
        metacat: fits_table
            configuration-like params for the simulated sources
        simcat: fits_table
            simulated source catalog for a given brick (not CCD).
        output_dir: legacypipe's outdir
        add_sim_noise: add Poisson noise from the simulated source to the image
        seed: for random number generators
        image_eq_model: referred to as 'testA'
            wherever add a simulated source, replace both image and invvar of the image
            with that of the simulated source only

    Attributes:
        DR: see above
        metacat: fits_table
            configuration-like params for the simulated sources
        simcat: fits_table
            simulated source catalog for a given brick (not CCD).
        output_dir: legacypipe's outdir
        add_sim_noise: add Poisson noise from the simulated source to the image
        image_eq_model: referred to as 'testA'
            wherever add a simulated source, replace both image and invvar of the image
            with that of the simulated source only
    """

    def __init__(self, dataset=None, survey_dir=None, metacat=None, simcat=None,
                 output_dir=None,add_sim_noise=False, seed=0,
                 image_eq_model=False,**kwargs):
        self.dataset= dataset
        kw= dict(survey_dir=survey_dir,
                 output_dir=output_dir)
        if self.dataset == 'cosmos':
            kw.update(subset=kwargs['subset'])
        super(SimDecals, self).__init__(**kw)

        self.metacat = metacat
        self.simcat = simcat
        # Additional options from command line
        self.add_sim_noise= add_sim_noise
        self.seed= seed
        self.image_eq_model= image_eq_model
        print('SimDecals: self.image_eq_model=',self.image_eq_model)

    def get_image_object(self, t):
        if self.dataset == 'cosmos':
            return SimImageCosmos(self, t)
        else:
            if t.camera=='decam':
               return SimImage(self, t)
            elif t.camera=='90prime':
                return SimImageBok(self, t)
            elif t.camera=='mosaic':
                return SimImageMosaic(self, t)
            else:
                raise ValueError('no such camera!%s'%t.camera)

    def filter_ccds_files(self, fns):
        """see legacypipe/runs.py"""
        return fns

    def ccds_for_fitting(self, brick, ccds):
        if self.dataset in ['dr3','dr5','dr8']:#dr9 is decam/90prime/mosaic
            return np.flatnonzero(ccds.camera == 'decam')
        elif self.dataset in ['cosmos']:
            return np.flatnonzero(ccds.camera == 'decam+noise')
        #elif self.dataset == 'DR4':
            #   return np.flatnonzero(np.logical_or(ccds.camera == 'mosaic',
        #                         ccds.camera == '90prime'))

    def filter_ccd_kd_files(self, fns):
        """see legacypipe/runs.py"""
        return []

def get_srcimg_invvar(stamp_ivar,img_ivar):
    """stamp_ivar, img_ivar -- galsim Image objects"""
    # Use img_ivar when stamp_ivar == 0, both otherwise
    use_img_ivar= np.ones(img_ivar.array.shape).astype(bool)
    use_img_ivar[ stamp_ivar.array > 0 ] = False
    # First compute using both
    ivar= np.power(stamp_ivar.array.copy(), -1) + np.power(img_ivar.array.copy(), -1)
    ivar= np.power(ivar,-1)
    keep= np.ones(ivar.shape).astype(bool)
    keep[ (stamp_ivar.array > 0)*\
          (img_ivar.array > 0) ] = False
    ivar[keep] = 0.
    # Now use img_ivar only where need to
    ivar[ use_img_ivar ] = img_ivar.array.copy()[ use_img_ivar ]
    # return
    obj_ivar = stamp_ivar.copy()
    obj_ivar.fill(0.)
    obj_ivar+= ivar
    return obj_ivar

def saturation_e(camera):
    # Saturation limit
    d=dict(decam=3e4) # e-
    return d[camera]

def ivar_to_var(ivar,nano2e=None,camera='decam'):
    assert(nano2e is not None)
    flag= ivar == 0.
    var= np.power(ivar, -1)
    # Set 0 ivar pixels to satuation limit
    # var * nano2e^2 = e-^2
    sat= saturation_e(camera) / nano2e**2
    var[flag]= sat
    return var
# except NameError:
#     pass

# try:
class SimDecalsCosmos(SimDecals,CosmosSurvey):
    """Filters the CCDs to just those in the cosmos realizations

    Call just like SimDecals except with additional Argument 'subset'

    Args:
        **kwargs: SimDecals args + 'subset'
    """

    def __init__(self, **kwargs):
        super(SimDecalsCosmos, self).__init__(**kwargs)
# except NameError:
#     pass


class SimImageMosaic(MosaicImage):
    def __init__(self, survey, t):
        super(SimImageMosaic, self).__init__(survey, t)
        self.t = t
        self.wtfn= (self.imgfn.replace('_oki_','_oow_')
                                  .replace('_ooi_','_oow_'))
        self.dqfn= self.wtfn.replace('_oow_','_ood_')
        self.cosky=False
        if self.cosky:
            self.get_cosky()
    def get_cosky(self):
        topdir='/global/cscratch1/sd/djschleg/dr9g/dr9g/north/coadd/'
        global BRICKNAME
        for band in ['g','r','z']:
            fn = os.path.join(topdir,BRICKNAME[:3],BRICKNAME,'legacysurvey-%s-image-%s.fits.fz'%(BRICKNAME,band))
            cosky = float(fits.open(fn)[1].header['COSKY_%s'%band.upper()])
            if band=='g':
                self.cosky_g=cosky
            if band=='r':
                self.cosky_r=cosky
            if band=='z':
                self.cosky_z=cosky


    def get_tractor_image(self, **kwargs):
        #t0 = time_builtin.clock()
        tim = super(SimImageMosaic, self).get_tractor_image(**kwargs)
        if tim is None: # this can be None when the edge of a CCD overlaps
            return tim
        #debug_hui
        #shot the image to 0
        tim.data = np.zeros(tim.data.shape)

        t1=time_builtin.clock()
        objtype = self.survey.metacat.get('objtype')[0]
        objstamp = BuildStamp(tim, seed=self.survey.seed,
                              camera=self.t.camera,
                              gain=tim.gain,exptime=self.t.exptime)
        # ids make it onto a ccd (geometry cut)
        tim.ids_added=[]

        # Grab the data and inverse variance images [nanomaggies!]
        tim_image = galsim.Image(tim.getImage())
        tim_invvar = galsim.Image(tim.getInvvar())
        tim_dq = galsim.Image(tim.dq)
        # Also store galaxy sims and sims invvar
        sims_image = tim_image.copy()
        sims_image.fill(0.0)
        sims_ivar = sims_image.copy()

        # Store simulated galaxy images in tim object
        # Loop on each object.
        #f = open(self.stamp_stat_fn,'a')
        for ii, obj in enumerate(self.survey.simcat):
            # Print timing
            t0= Time()
            if objtype in ['lrg','elg']:
                obj.n = int(obj.n)
                strin= 'Drawing 1 %s: n=%.2f, rhalf=%.2f, e1=%.2f, e2=%.2f' % \
                        (objtype.upper(), obj.n,obj.rhalf,obj.e1,obj.e2)
                print(strin)

            if objtype == 'star':
                stamp = objstamp.star(obj)
            elif objtype == 'elg':
                stamp = objstamp.elg(obj)
            elif objtype == 'lrg':
                stamp = objstamp.lrg(obj)
            elif objtype == 'qso':
                stamp = objstamp.qso(obj)
            t0= ptime('Finished Drawing %s: id=%d band=%s dbflux=%f addedflux=%f' %
                (objtype.upper(), obj.id,objstamp.band,
                 obj.get(objstamp.band+'flux'),stamp.array.sum()), t0)
            stamp_nonoise= stamp.copy()
            if self.survey.add_sim_noise:
                stamp += noise_for_galaxy(stamp,objstamp.nano2e)
            ivarstamp= ivar_for_galaxy(stamp,objstamp.nano2e)
            # Add source if EVEN 1 pix falls on the CCD
            overlap = stamp.bounds & tim_image.bounds
            if overlap.area() > 0:
                print('Stamp overlaps tim: id=%d band=%s' % (obj.id,objstamp.band))
                tim.ids_added.append(obj.id)
                stamp = stamp[overlap]
                if self.cosky:
                    if tim.band=='g':
                        stamp_offset = galsim.Image(np.ones_like(stamp.array)*self.cosky_g)
                        stamp+=stamp_offset
                    if tim.band=='r':
                        stamp_offset = galsim.Image(np.ones_like(stamp.array)*self.cosky_r)
                        stamp+=stamp_offset
                    if tim.band=='z':
                        stamp_offset = galsim.Image(np.ones_like(stamp.array)*self.cosky_z)
                        stamp+=stamp_offset
                ivarstamp = ivarstamp[overlap]
                stamp_nonoise= stamp_nonoise[overlap]

                # Zero out invvar where bad pixel mask is flagged (> 0)
                keep = np.ones(tim_dq[overlap].array.shape)
                keep[ tim_dq[overlap].array > 0 ] = 0.
                ivarstamp *= keep
                # Add stamp to image
                back= tim_image[overlap].copy()
                tim_image[overlap] += stamp #= back.copy() + stamp.copy()
                #print('this is mosaic')
                #import pdb;pdb.set_trace()
                #print(objstamp.xpos,objstamp.ypos)
                #print(stamp.array.max())
                #print(tim_image[objstamp.ypos][objstamp.xpos])
                # Add variances
                back_ivar= tim_invvar[overlap].copy()
                tot_ivar= get_srcimg_invvar(ivarstamp, back_ivar)
                tim_invvar[overlap] = tot_ivar.copy()

                #Extra
                sims_image[overlap] += stamp.copy()
                sims_ivar[overlap] += ivarstamp.copy()

                if np.min(sims_ivar.array) < 0:
                    log.warning('Negative invvar!')
                    import pdb ; pdb.set_trace()
        #f.close()
        tim.sims_image = sims_image.array
        tim.sims_inverr = np.sqrt(sims_ivar.array)
        # Can set image=model, ivar=1/model for testing
        if self.survey.image_eq_model:
            tim.data = sims_image.array.copy()
            tim.inverr = np.zeros(tim.data.shape)
            tim.inverr[sims_image.array > 0.] = np.sqrt(1./sims_image.array.copy()[sims_image.array > 0.])
        else:
            tim.data = tim_image.array
            tim.inverr = np.sqrt(tim_invvar.array)
        sys.stdout.flush()
        #print('get_tractor_image in obiwan :time '+str(time_builtin.clock()-t1))
        return tim

class SimImageBok(BokImage):
    def __init__(self, survey, t):
        super(SimImageBok, self).__init__(survey, t)
        self.t = t
        self.wtfn= (self.imgfn.replace('_oki_','_oow_')
                                  .replace('_ooi_','_oow_'))
        self.dqfn= self.wtfn.replace('_oow_','_ood_')
        self.cosky=False
        if self.cosky:
            self.get_cosky()
    def get_cosky(self):
        topdir='/global/cscratch1/sd/djschleg/dr9g/dr9g/north/coadd/'
        global BRICKNAME
        for band in ['g','r','z']:
            fn = os.path.join(topdir,BRICKNAME[:3],BRICKNAME,'legacysurvey-%s-image-%s.fits.fz'%(BRICKNAME,band))
            cosky = float(fits.open(fn)[1].header['COSKY_%s'%band.upper()])
            if band=='g':
                self.cosky_g=cosky
            if band=='r':
                self.cosky_r=cosky
            if band=='z':
                self.cosky_z=cosky
    def get_tractor_image(self, **kwargs):
        #t0 = time_builtin.clock()
        tim = super(SimImageBok, self).get_tractor_image(**kwargs)
        if tim is None: # this can be None when the edge of a CCD overlaps
            return tim
        #debug_hui
        #shot the image to 0
        tim.data = np.zeros_like(tim.data)

        t1=time_builtin.clock()
        objtype = self.survey.metacat.get('objtype')[0]
        objstamp = BuildStamp(tim, seed=self.survey.seed,
                              camera=self.t.camera,
                              gain=tim.gain,exptime=self.t.exptime)
        # ids make it onto a ccd (geometry cut)
        tim.ids_added=[]

        # Grab the data and inverse variance images [nanomaggies!]
        tim_image = galsim.Image(tim.getImage())
        tim_invvar = galsim.Image(tim.getInvvar())
        tim_dq = galsim.Image(tim.dq)
        # Also store galaxy sims and sims invvar
        sims_image = tim_image.copy()
        sims_image.fill(0.0)
        sims_ivar = sims_image.copy()

        # Store simulated galaxy images in tim object
        # Loop on each object.
        #f = open(self.stamp_stat_fn,'a')
        for ii, obj in enumerate(self.survey.simcat):
            # Print timing
            t0= Time()
            if objtype in ['lrg','elg']:
                obj.n = int(obj.n)
                strin= 'Drawing 1 %s: n=%.2f, rhalf=%.2f, e1=%.2f, e2=%.2f' % \
                        (objtype.upper(), obj.n,obj.rhalf,obj.e1,obj.e2)
                print(strin)

            if objtype == 'star':
                stamp = objstamp.star(obj)
            elif objtype == 'elg':
                stamp = objstamp.elg(obj)
            elif objtype == 'lrg':
                stamp = objstamp.lrg(obj)
            elif objtype == 'qso':
                stamp = objstamp.qso(obj)
            t0= ptime('Finished Drawing %s: id=%d band=%s dbflux=%f addedflux=%f' %
                (objtype.upper(), obj.id,objstamp.band,
                 obj.get(objstamp.band+'flux'),stamp.array.sum()), t0)
            stamp_nonoise= stamp.copy()
            if self.survey.add_sim_noise:
                stamp += noise_for_galaxy(stamp,objstamp.nano2e)
            ivarstamp= ivar_for_galaxy(stamp,objstamp.nano2e)
            # Add source if EVEN 1 pix falls on the CCD
            overlap = stamp.bounds & tim_image.bounds
            if overlap.area() > 0:

                print('Stamp overlaps tim: id=%d band=%s' % (obj.id,objstamp.band))
                tim.ids_added.append(obj.id)
                stamp = stamp[overlap]
                ivarstamp = ivarstamp[overlap]
                stamp_nonoise= stamp_nonoise[overlap]  
                if self.cosky:
                    if tim.band=='g':
                        stamp_array=galsim.Image(np.ones_like(stamp.array)*self.cosky_g)
                        stamp+=stamp_array
                    if tim.band=='r':
                        stamp_array=galsim.Image(np.ones_like(stamp.array)*self.cosky_r)
                        stamp+=stamp_array
                    if tim.band=='z':
                        stamp_array=galsim.Image(np.ones_like(stamp.array)*self.cosky_z)
                        stamp+=stamp_array
                
                # Zero out invvar where bad pixel mask is flagged (> 0)
                keep = np.ones(tim_dq[overlap].array.shape)
                keep[ tim_dq[overlap].array > 0 ] = 0.
                ivarstamp *= keep

                # Add stamp to image
                back= tim_image[overlap].copy()
                tim_image[overlap] += stamp #= back.copy() + stamp.copy()
                global BRICKNAME
                global SURVEY_DIR
                survey = LegacySurveyData(survey_dir=SURVEY_DIR)
                brickinfo= get_brickinfo_hack(survey,BRICKNAME)
                brickwcs = wcs_for_brick(brickinfo)
                W, H, pixscale = brickwcs.get_width(), brickwcs.get_height(), brickwcs.pixel_scale()
                get_offset_image(tim,objstamp.target_x,objstamp.target_y,stamp.array.max(),tim_image.array)
                print('this is bok image')
                brick00 = survey.get_brick_by_name(BRICKNAME)
                targetwcs = wcs_for_brick(brick00, W=W, H=H, pixscale=pixscale)
                import pdb;pdb.set_trace()
                flag, xx, yy = tim.subwcs.radec2pixelxy(*(targetwcs.pixelxy2radec(objstamp.target_x+1,objstamp.target_x+1)[-2:]))
                print(xx,yy)
                print(objstamp.xpos,objstamp.ypos)
                print(stamp.array.max())
                print(tim_image.array[objstamp.ypos][objstamp.xpos])
                get_offset_check(tim,objstamp.xpos,objstamp.ypos)
                tim_image = galsim.Image(np.zeros_like(tim.data))
                # Add variances
                back_ivar= tim_invvar[overlap].copy()
                tot_ivar= get_srcimg_invvar(ivarstamp, back_ivar)
                tim_invvar[overlap] = tot_ivar.copy()

                #Extra
                sims_image[overlap] += stamp.copy()
                sims_ivar[overlap] += ivarstamp.copy()
                
                if np.min(sims_ivar.array) < 0:
                    log.warning('Negative invvar!')
                    import pdb ; pdb.set_trace()


        #f.close()
        tim.sims_image = sims_image.array
        tim.sims_inverr = np.sqrt(sims_ivar.array)
        # Can set image=model, ivar=1/model for testing
        if self.survey.image_eq_model:
            tim.data = sims_image.array.copy()
            tim.inverr = np.zeros(tim.data.shape)
            tim.inverr[sims_image.array > 0.] = np.sqrt(1./sims_image.array.copy()[sims_image.array > 0.])
        else:
            tim.data = tim_image.array
            tim.inverr = np.sqrt(tim_invvar.array)
        sys.stdout.flush()
        #print('get_tractor_image in obiwan :time '+str(time_builtin.clock()-t1))
        
        return tim


def get_offset_image(tim,x_cen,y_cen,value,img):
        #Jun9 input x,y coordinat, output x,y on coimg
        global BRICKNAME
        global SURVEY_DIR
        survey = LegacySurveyData(survey_dir=SURVEY_DIR)
        brickinfo= get_brickinfo_hack(survey,BRICKNAME)
        brickwcs = wcs_for_brick(brickinfo)
        W, H, pixscale = brickwcs.get_width(), brickwcs.get_height(), brickwcs.pixel_scale()
        brick00 = survey.get_brick_by_name(BRICKNAME)
        targetwcs = wcs_for_brick(brick00, W=W, H=H, pixscale=pixscale)
        from astrometry.util.miscutils import patch_image
        from astrometry.util.resample import resample_with_wcs
        #set the center pixel to 100
        #h,w = int(tim.subwcs.imageh), int(tim.subwcs.imagew)
        #y_cen = int(h*np.random.uniform())
        #x_cen = int(w*np.random.uniform())
        imgs = [img]
        Yo,Xo,Yi,Xi,rimgs = resample_with_wcs(targetwcs, tim.subwcs, imgs, 1, intType=np.int16)
        coimg = np.zeros((int(H),int(W)), np.float32)
        coimg[Yo,Xo] += img[Yi,Xi]
        out = np.where(coimg==coimg.max())
        yloc_out = out[0].mean()
        xloc_out = out[1].mean()
        flag, xx, yy = targetwcs.radec2pixelxy(*(tim.subwcs.pixelxy2radec(float(x_cen+1), float(y_cen+1))[-2:]))
        if flag:
            print('x_cen:%d, y_cen:%d'%(x_cen,y_cen))
            print('x real:%d, y real:%d'%(xloc_out,yloc_out))
            #print('x theory:%.1f, y theory:%.1f'%(xx-1,yy-1))

def get_offset_check(tim,x_cen,y_cen):
        #Jun9 input x,y coordinat, output x,y on coimg
        global BRICKNAME
        global SURVEY_DIR
        survey = LegacySurveyData(survey_dir=SURVEY_DIR)
        brickinfo= get_brickinfo_hack(survey,BRICKNAME)
        brickwcs = wcs_for_brick(brickinfo)
        W, H, pixscale = brickwcs.get_width(), brickwcs.get_height(), brickwcs.pixel_scale()
        brick00 = survey.get_brick_by_name(BRICKNAME)
        targetwcs = wcs_for_brick(brick00, W=W, H=H, pixscale=pixscale)
        from astrometry.util.miscutils import patch_image
        from astrometry.util.resample import resample_with_wcs
        metric_img = np.zeros_like(tim.data)
        #set the center pixel to 100
        #h,w = int(tim.subwcs.imageh), int(tim.subwcs.imagew)
        #y_cen = int(h*np.random.uniform())
        #x_cen = int(w*np.random.uniform())
        metric_img[y_cen][x_cen] = 100
        patched = metric_img
        imgs = [patched]
        Yo,Xo,Yi,Xi,rimgs = resample_with_wcs(targetwcs, tim.subwcs, imgs, 1, intType=np.int16)
        coimg = np.zeros((int(H),int(W)), np.float32)
        coimg[Yo,Xo] += patched[Yi,Xi]
        out = np.where(coimg==coimg.max())
        yloc_out = out[0].mean()
        xloc_out = out[1].mean()
        flag, xx, yy = targetwcs.radec2pixelxy(*(tim.subwcs.pixelxy2radec(float(x_cen+1), float(y_cen+1))[-2:]))
        if flag:
            print('x_cen:%d, y_cen:%d'%(x_cen,y_cen))
            print('x real:%d, y real:%d'%(xloc_out,yloc_out))
            print('x theory:%.1f, y theory:%.1f'%(xx-1,yy-1))

# try:
class SimImage(DecamImage):
    """Adds simulated sources to a single exposure

    Similar behavior as legacypipe.decam.DecamImage. Instead of
        loading images specifically from DECam,this  loads images
        with simulated sources added in

    Args:
        survey: SimDecals() object
        t: as used by DecamImage
            a single row fits_table for a specific CCD

    Attributes:
        inherits: DecamImage
        t: as used by DecamImage
            a single row fits_table for a specific CCD
    """

    def __init__(self, survey, t):
        super(SimImage, self).__init__(survey, t)
        self.t = t
        # Find image on proj or proja if doesn't exist
        #base_fn = os.path.basename(self.imgfn) 
        #date_fn = os.path.basename(os.path.dirname(self.imgfn))[:10]
        #found = glob('/global/project/projectdirs/cosmo/staging/decam/CP/*/%s/%s'%(date_fn,base_fn), recursive=False)
        #if len(found) == 0:
        #    found = glob('/global/project/projectdirs/cosmo/staging/decam/NonDECaLS/%s/%s'%(date_fn,base_fn), recursive=False)
        #if len(found) == 0:
        #    print('%s not found'%(self.imgfn))
        #    raise
        #else:
        #    self.imgfn= found[0]
        #sys.stdout.flush()
        self.wtfn= (self.imgfn.replace('_oki_','_oow_')
                                  .replace('_ooi_','_oow_'))
        self.dqfn= self.wtfn.replace('_oow_','_ood_')

    def get_tractor_image(self, **kwargs):
        #t0 = time_builtin.clock()
        tim = super(SimImage, self).get_tractor_image(**kwargs)
        if tim is None: # this can be None when the edge of a CCD overlaps
            return tim
        #debug_hui
        #shot the image to 0
        #tim.data = np.zeros(tim.data.shape)

        t1=time_builtin.clock()
        objtype = self.survey.metacat.get('objtype')[0]
        objstamp = BuildStamp(tim, seed=self.survey.seed,
                              camera=self.t.camera,
                              gain=tim.gain,exptime=self.t.exptime)
        # ids make it onto a ccd (geometry cut)
        tim.ids_added=[]

        # Grab the data and inverse variance images [nanomaggies!]
        tim_image = galsim.Image(tim.getImage())
        tim_invvar = galsim.Image(tim.getInvvar())
        tim_dq = galsim.Image(tim.dq)
        # Also store galaxy sims and sims invvar
        sims_image = tim_image.copy()
        sims_image.fill(0.0)
        sims_ivar = sims_image.copy()

        # Store simulated galaxy images in tim object
        # Loop on each object.
        #f = open(self.stamp_stat_fn,'a')
        for ii, obj in enumerate(self.survey.simcat):
            # Print timing
            t0= Time()
            if objtype in ['lrg','elg']:
                obj.n = int(obj.n)
                strin= 'Drawing 1 %s: n=%.2f, rhalf=%.2f, e1=%.2f, e2=%.2f' % \
                        (objtype.upper(), obj.n,obj.rhalf,obj.e1,obj.e2)
                print(strin)

            if objtype == 'star':
                stamp = objstamp.star(obj)
            elif objtype == 'elg':
                stamp = objstamp.elg(obj)
            elif objtype == 'lrg':
                stamp = objstamp.lrg(obj)
            elif objtype == 'qso':
                stamp = objstamp.qso(obj)
            t0= ptime('Finished Drawing %s: id=%d band=%s dbflux=%f addedflux=%f' %
                (objtype.upper(), obj.id,objstamp.band,
                 obj.get(objstamp.band+'flux'),stamp.array.sum()), t0)
            stamp_nonoise= stamp.copy()
            if self.survey.add_sim_noise:
                stamp += noise_for_galaxy(stamp,objstamp.nano2e)
            ivarstamp= ivar_for_galaxy(stamp,objstamp.nano2e)
            # Add source if EVEN 1 pix falls on the CCD
            overlap = stamp.bounds & tim_image.bounds
            if overlap.area() > 0:
                print('Stamp overlaps tim: id=%d band=%s' % (obj.id,objstamp.band))
                tim.ids_added.append(obj.id)
                stamp = stamp[overlap]
                ivarstamp = ivarstamp[overlap]
                stamp_nonoise= stamp_nonoise[overlap]

                # Zero out invvar where bad pixel mask is flagged (> 0)
                keep = np.ones(tim_dq[overlap].array.shape)
                keep[ tim_dq[overlap].array > 0 ] = 0.
                ivarstamp *= keep
                # Add stamp to image
                back= tim_image[overlap].copy()
                tim_image[overlap] += stamp #= back.copy() + stamp.copy()
                # Add variances
                back_ivar= tim_invvar[overlap].copy()
                tot_ivar= get_srcimg_invvar(ivarstamp, back_ivar)
                tim_invvar[overlap] = tot_ivar.copy()

                #Extra
                sims_image[overlap] += stamp.copy()
                sims_ivar[overlap] += ivarstamp.copy()

                if np.min(sims_ivar.array) < 0:
                    log.warning('Negative invvar!')
                    import pdb ; pdb.set_trace()
        #f.close()
        tim.sims_image = sims_image.array
        tim.sims_inverr = np.sqrt(sims_ivar.array)
        # Can set image=model, ivar=1/model for testing
        if self.survey.image_eq_model:
            tim.data = sims_image.array.copy()
            tim.inverr = np.zeros(tim.data.shape)
            tim.inverr[sims_image.array > 0.] = np.sqrt(1./sims_image.array.copy()[sims_image.array > 0.])
        else:
            tim.data = tim_image.array
            tim.inverr = np.sqrt(tim_invvar.array)
        sys.stdout.flush()
        #print('get_tractor_image in obiwan :time '+str(time_builtin.clock()-t1))
        return tim
# except NameError:
#     pass

# try:
class SimImageCosmos(SimImage,DecamImagePlusNoise):
    """Filters the CCDs to just those in the cosmos realizations

    Call just like SimDecals except with additional Argument 'subset'

    Args:
        **kwargs: SimDecals args + 'subset'
    """

    def __init__(self, survey, t):
        super(SimImageCosmos, self).__init__(survey, t)
# except NameError:
#     pass

def noise_for_galaxy(gal,nano2e):
    """Returns numpy array of noise in Img count units for gal in image cnt units"""
    # Noise model + no negative image vals when compute noise
    one_std_per_pix= gal.array.copy() # nanomaggies
    one_std_per_pix[one_std_per_pix < 0]=0
    # rescale
    one_std_per_pix *= nano2e # e-
    one_std_per_pix= np.sqrt(one_std_per_pix)
    num_stds= np.random.randn(one_std_per_pix.shape[0],one_std_per_pix.shape[1])
    #one_std_per_pix.shape, num_stds.shape
    noise= one_std_per_pix * num_stds
    # rescale
    noise /= nano2e #nanomaggies
    return noise

def ivar_for_galaxy(gal,nano2e):
    """Adds gaussian noise to perfect source

    Args:
        gal: galsim.Image() for source, UNITS: nanomags
        nano2e: factor to convert to e- (gal * nano2e has units e-)

    Returns:
        galsim.Image() of invvar for the source, UNITS: nanomags
    """
    var= gal.copy() * nano2e #e^2
    var.applyNonlinearity(np.abs)
    var /= nano2e**2 #nanomag^2
    var.invertSelf()
    return var




class BuildStamp_old():

      """
          this version of BuildStamp draws tractor model galaxies instead of GalSim Galaxies
          Does the drawing of simulated sources on a single exposure
          Args:
          tim: Tractor Image Object for a specific CCD
          gain: gain of the CCD
      """
      def __init__(self,tim,seed=0,camera=None,gain=None,exptime=None): #I think tim is the only useful thing here
          self.band = tim.band.strip()
          self.zpscale = tim.zpscale      # nanomaggies-->ADU (decam) or e-/sec (bass,mzls)
          assert(camera in ['decam','mosaic','90prime'])
          if camera == 'decam':
               self.nano2e = self.zpscale*gain
          else:
                self.nano2e = self.zpscale
          self.tim = tim
      def setlocal(self,obj):#get a sub_tim image as what's been done in fitblobs stage. see _blob_iter? code for details
              """Get the pixel positions, local wcs, local PSF."""
              #changed to fit for what's been done in fitblobs stage
              #center value of ra,dec for input image
              ra_cen,dec_cen = obj.get('ra'), obj.get('dec')
              #changed to pixel value x,y
              (flag, x_cen,y_cen) = self.tim.subwcs.radec2pixelxy(ra_cen,dec_cen)
              x_cen-=1
              y_cen-=1
              self.wcs=self.tim.getWcs() 
              #size of the stamp is 64*64, so get an image of the same size, unless it's around the edge
              x_cen_int,y_cen_int = round(x_cen),round(y_cen)
              #note: boundary need to be checked to see if it's consistent !TODO
              self.sx0,self.sx1,self.sy0,self.sy1 = x_cen-32,x_cen+31,y_cen-32,y_cen+31
              (h,w) = self.tim.shape
              self.sx0 = np.clip(int(self.sx0), 0, w-1)
              self.sx1 = np.clip(int(self.sx1), 0, w-1) + 1
              self.sy0 = np.clip(int(self.sy0), 0, h-1)
              self.sy1 = np.clip(int(self.sy1), 0, h-1) + 1
              subslc = slice(self.sy0,self.sy1),slice(self.sx0,self.sx1)
              subimg = self.tim.getImage ()[subslc]
              subie  = self.tim.getInvError()[subslc]
              subwcs = self.tim.getWcs().shifted(self.sx0, self.sy0)
              subsky = self.tim.getSky().shifted(self.sx0, self.sy0)
              subpsf = self.tim.psf.constantPsfAt((self.sx0+self.sx1)/2., (self.sy0+self.sy1)/2.)
              new_tim = tractor.Image(data=subimg, inverr=subie, wcs=subwcs,psf=subpsf, photocal=self.tim.getPhotoCal(), sky=subsky, name=self.tim.name)
              return new_tim
      def elg(self,obj):
          new_tim = self.setlocal(obj)
          n,ra,dec,r_half,e1,e2,flux = int(obj.get('n')),float(obj.get('ra')),float(obj.get('dec')),float(obj.get('rhalf')),float(obj.get('e1')),float(obj.get('e2')),float(obj.get(self.band+'flux'))
          log_r_half = np.log(r_half)
          assert(self.band in ['g','r','z'])
          if self.band == 'g':
               brightness =  tractor.NanoMaggies(g=flux, order=['g'])
          if self.band == 'r':
                brightness =  tractor.NanoMaggies(r=flux, order=['r'])
          if self.band == 'z':
                brightness =  tractor.NanoMaggies(z=flux, order=['z'])

          shape = LegacyEllipseWithPriors(log_r_half, e1, e2)
          if n==1:
              new_gal = ExpGalaxy(RaDecPos(ra,dec),brightness,shape)
          elif n==4:
              new_gal = DevGalaxy(RaDecPos(ra,dec),brightness,shape)
          else:
               raise
          new_tractor = Tractor([new_tim], [new_gal])
          mod0 = new_tractor.getModelImage(0)
          galsim_img = galsim.Image(mod0)
          galsim_img.bounds.xmin=self.sx0
          galsim_img.bounds.xmax=self.sx1-1
          galsim_img.bounds.ymin=self.sy0
          galsim_img.bounds.ymax=self.sy1-1
          return galsim_img





class BuildStamp():
    """Does the drawing of simulated sources on a single exposure

    Args:
        tim: Tractor Image Object for a specific CCD
        gain: gain of the CCD

    Attributes:
        band: g,r,z
        camera: 'decam', 'mosaic', '90prime'
        gsparams: galsim object that configures how accurate simulated source will be
        gsdeviate: galsim object that configures its random number generator
        wcs: WCS from tim
        psf: psf from tim
        galsim_wcs: wcs repackaged into galsim compatible object
        zpscale: conversion factor 'nanomaggies' to 'Image units used by Legacypipe', which
            are ADU/sec for DECam and e/sec for Bass,MzLS
        nano2e: conversion factor 'nanomaggies' to 'e-'
    """

    def __init__(self,tim, seed=0,
                 camera=None,gain=None,exptime=None):
        #self.camera=camera
        self.band = tim.band.strip()
        # GSParams should be used when galsim object is initialized
        # MAX size for sersic n < 6.2
        # https://github.com/GalSim-developers/GalSim/pull/450/commits/755bcfdca25afe42cccfd6a7f8660da5ecda2a65
        self.gsparams = galsim.GSParams(maximum_fft_size=65536)
        #print('FIX ME!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
        self.gsdeviate = galsim.BaseDeviate(seed)
        self.wcs = tim.getWcs()
        self.psf = tim.getPsf()

        # zpscale equivalent to magzpt = self.t.ccdzpt+2.5*np.log10(self.t.exptime)
        self.zpscale = tim.zpscale      # nanomaggies-->ADU (decam) or e-/sec (bass,mzls)
        assert(camera in ['decam','mosaic','90prime'])
        if camera == 'decam':
            self.nano2e = self.zpscale*gain # nanomaggies -> ADU -> e-
        else:
            # correct for mzls, possibly not for bass
            self.nano2e = self.zpscale # nanomaggies -> e-/s -> e-
        self.tim=tim
        

        global BRICKNAME
        global SURVEY_DIR
        survey = LegacySurveyData(survey_dir=SURVEY_DIR)
        brickinfo= get_brickinfo_hack(survey,BRICKNAME)
        brickwcs = wcs_for_brick(brickinfo)
        W, H, pixscale = brickwcs.get_width(), brickwcs.get_height(), brickwcs.pixel_scale()
        brick00 = survey.get_brick_by_name(BRICKNAME)
        targetwcs = wcs_for_brick(brick00, W=W, H=H, pixscale=pixscale)
        self.targetwcs = targetwcs
        #from astrometry.util.miscutils import patch_image
        #from astrometry.util.resample import resample_with_wcs
        #patched = self.tim.getImage().copy()
        #imgs = [patched]
        #Yo,Xo,Yi,Xi,rimgs = resample_with_wcs(targetwcs, self.tim.subwcs, imgs, 1, intType=np.int16)
        #self.Yo=Yo
        #self.Xo=Xo
        #self.Yi=Yi
        #self.Xi=Xi
        #jun8
        #flag, xx,yy=self.targetwcs.radec2pixelxy(*(self.tim.subwcs.pixelxy2radec(float(0 + 1), float(0 + 1)))[-2:])
        #self.tim.data[0:,0]=100+xx
        #self.tim.data.transpose()[0:,0]=150+yy
        #self.tim.data[0:,-1]=100+xx
        #self.tim.data.transpose()[0:,-1]=100+yy

    def setlocal(self,obj):
        """Get the pixel positions, local wcs, local PSF."""
        x=int(obj.get('x')+0.5)
        y=int(obj.get('y')+0.5)
        self.target_x=x
        self.target_y=y
        flag, xx, yy = self.tim.subwcs.radec2pixelxy(*(self.targetwcs.pixelxy2radec(x+1,y+1)[-2:]))
        #flag, xx, yy = self.tim.subwcs.radec2pixelxy(obj.get('ra'),obj.get('dec'))
        xx-=1
        yy-=1
        
        self.pos = galsim.PositionD(xx, yy)
        self.xpos = int(self.pos.x+0.5)
        self.ypos = int(self.pos.y+0.5)
        #testing on if corrdinate conversion is correct
        #x = obj.get('x')
        #y = obj.get('y')
        #self.coimg_x=x
        #self.coimg_y=y
        #try:
        #    get_offset_check(self.tim,self.xpos,self.ypos)
        #    print(self.coimg_x,self.coimg_y)
        #except:
        #    pass
        self.offset = galsim.PositionD(self.pos.x-self.xpos, self.pos.y-self.ypos)

        # Get the local PSF
        self.localpsf = self.psf.getPointSourcePatch(self.xpos, self.ypos)
        self.localpsf= galsim.Image(self.localpsf.getImage(),
                                    scale=self.wcs.pixscale_at(self.xpos,self.ypos))

    def star(self,obj):
        """Render a star (PSF)."""
        log = logging.getLogger('decals_sim')
        # Use input flux as the 7'' aperture flux
        self.setlocal(obj)
        stamp = self.localpsf.copy()
        stamp.shift(dx=self.offset.x,dy=self.offset.y)
        # Scale to desired flux
        stamp *= float(obj.get(self.band+'flux')) # [nanomaggies]
        # position in observed image
        stamp.setCenter(self.xpos, self.ypos)
        return stamp


    def convolve_galaxy(self,gal):
        """Convolve the object with the PSF and then draw it."""
        psf= self.localpsf.copy()
        # doesn't change tractor measurements
        #psf /= psf.array.sum()
        psf= galsim.InterpolatedImage(psf)
                                             #gsparams=self.gsparams)
        return galsim.Convolve([gal, psf]) #, gsparams=self.gsparams)

    def elg(self,obj):
        """Create an ELG (disk-like) galaxy."""
        # Create localpsf object
        self.setlocal(obj)
        #try:
        # TRIAL: galaxy profile
        gal = galsim.Sersic(float(obj.get('n')),
                    half_light_radius=float(obj.get('rhalf')),\
                    flux=float(obj.get(self.band+'flux')),
                    gsparams=self.gsparams)
        gal = gal.shear(e1=float(obj.get('e1')), e2=float(obj.get('e2')))
        # Convolve with normed-psf
        gal = self.convolve_galaxy(gal)
        gal = gal.drawImage(method='auto',
                            offset=self.offset,
                            scale= self.wcs.pixscale_at(self.xpos,self.ypos))
                            #method='no_pixel'
        # Scale to desired flux
        print('DREW galaxy, flux input=%.4f, actual=%.4f' % \
                (float(obj.get(self.band+'flux')),gal.array.sum()))
        # position in observed image
        #somehow this has to be -1 to match what we want
        gal.setCenter(self.xpos+1, self.ypos+1)
        return gal

    def lrg(self,obj):
        """Create an LRG just like did for ELG"""
        return self.elg(obj)

    def qso(self,obj):
        """Create a QSO just like a star"""
        return self.star(obj)



def flag_nearest_neighbors(Samp, radius_in_deg=5./3600):
  """Returns Sample indices to keep (have > dist separations) and indices to skip

  Returns:
    tuple: keep,skip: indices of Samp to keep and skip
  """
  flag_set=set()
  all_indices= range(len(Samp))
  for cnt in all_indices:
      if cnt in flag_set:
          continue
      else:
          I,J,d = match_radec(Samp.ra[cnt],Samp.dec[cnt],
                              Samp.ra,Samp.dec, 5./3600,
                              notself=False,nearest=False)
          # Remove all Samp matches (J), minus the reference ra,dec
          flag_inds= set(J).difference(set( [cnt] ))
          if len(flag_inds) > 0:
              flag_set= flag_set.union(flag_inds)
  keep= list( set(all_indices).difference(flag_set) )
  return keep, list(flag_set)

def get_ellip(q):
    """Given minor to major axis ratio (q) Returns ellipticity"""
    return (1-q**2)/(1+q**2)

def get_e1_e2(q,beta):
    """Given minor to major axis ratio (q) and postion angle (beta), Returns e1,e2 tuple"""
    e= get_ellip(q)
    return e*np.cos(2*beta), e*np.sin(2*beta)

#def build_simcat(nobj=None, brickname=None, brickwcs=None, meta=None, seed=None, noOverlap=True):
def build_simcat(Samp=None,brickwcs=None, meta=None):
    """Creates the simulated source catalog for a given brick (not CCD).

    The WCS for the brick (not CCD) is used to convert ra,dec of source
        to x,y pixel location in brickspace

    Args:
        Samp: fits_table for the properties of sources in the brick
            usually a subset of all sources in the brick determined by
            rowstart (rs)
        brickwcs: WCS object for the brick
        meta: 'metacat' table
            fits_table with configuration-like params for the simulated sources

    Returns:
        tuple of
        cat:
        skipping_ids:
    """
    log = logging.getLogger('decals_sim')

    #rand = np.random.RandomState(seed)

    # Assign central coordinates uniformly but remove simulated sources which
    # are too near to one another.  Iterate until we have the requisite number
    # of objects.
    #bounds = brickwcs.radec_bounds()
    #ra = rand.uniform(bounds[0], bounds[1], nobj)
    #dec = rand.uniform(bounds[2], bounds[3], nobj)
    i_keep,i_skip= flag_nearest_neighbors(Samp, radius_in_deg=5./3600)
    skipping_ids= Samp.get('id')[i_skip]
    log.info('sources %d, keeping %d, flagged as nearby %d' % (len(Samp),len(i_keep),len(i_skip)))
    Samp.cut(i_keep)

    #xxyy = brickwcs.radec2pixelxy(Samp.ra,Samp.dec)

    #cat = Table()
    #cat['ID'] = Column(Samp.get('id'),dtype='i4') #np.arange(nobj, dtype='i4'))
    #cat['RA'] = Column(Samp.ra, dtype='f8')
    #cat['DEC'] = Column(Samp.dec, dtype='f8')
    #cat['X'] = Column(xxyy[1][:], dtype='f4')
    #cat['Y'] = Column(xxyy[2][:], dtype='f4')
    global BRICKNAME
    global SURVEY_DIR
    survey = LegacySurveyData(survey_dir=SURVEY_DIR)
    brickinfo= get_brickinfo_hack(survey,BRICKNAME)
    brickwcs = wcs_for_brick(brickinfo)
    W, H, pixscale = brickwcs.get_width(), brickwcs.get_height(), brickwcs.pixel_scale()
    brick00 = survey.get_brick_by_name(BRICKNAME)
    targetwcs = wcs_for_brick(brick00, W=W, H=H, pixscale=pixscale)
    flag, xx,yy = targetwcs.radec2pixelxy(Samp.ra,Samp.dec)

    cat = fits_table()
    for key in ['id','ra','dec']:
        cat.set(key, Samp.get(key))
    cat.set('x', xx-1)
    cat.set('y', yy-1)

    typ=meta.get('objtype')[0]
    # Mags
    filts = ['%s %s' % ('DES', f) for f in 'grz']
    for band in ['g','r','z']:
        nanomag= 1E9*10**(-0.4*Samp.get(band))
        # Add extinction (to stars too, b/c "decam-chatter 6517")
        mw_transmission= SFDMap().extinction(['DES %s' % band],
                                             Samp.ra, Samp.dec)
        mw_transmission= 10**(-mw_transmission[:,0].astype(np.float32)/2.5)
        cat.set('%sflux' % band, nanomag * mw_transmission)
        cat.set('mw_transmission_%s' % band, mw_transmission)

    # Galaxy Properties
    if typ in ['elg','lrg']:
        # Convert to e1,e2 if given ba,pa
        if ('ba' in Samp.get_columns()) & ('pa' in Samp.get_columns()):
            e1,e2= get_e1_e2(Samp.get('ba'),Samp.get('pa'))
            Samp.set('e1',e1)
            Samp.set('e2',e2)
        for key in ['n','rhalf','e1','e2']:
            cat.set(key, Samp.get(key))
        # Sersic n: GALSIM n = [0.3,6.2] for numerical stability,see
        # https://github.com/GalSim-developers/GalSim/issues/{325,450}
    return cat, skipping_ids



def get_parser():
    '''return parser object, tells it what options to look for
    options can come from a list of strings or command line'''
    parser = argparse.ArgumentParser(formatter_class=argparse.
                                     ArgumentDefaultsHelpFormatter,
                                     description='DECaLS simulations.')
    parser.add_argument('--dataset', type=str, choices=['dr8','dr9', 'cosmos'], required=True, help='see definitions in obiwan/test/README.md')
    parser.add_argument('-o', '--objtype', type=str, choices=['star','elg', 'lrg', 'qso'], default='star', required=True)
    parser.add_argument('-b', '--brick', type=str, default='2428p117', required=True)
    parser.add_argument('--outdir', default='./', required=False)
    parser.add_argument('--logfn', default='./', required=False)
    parser.add_argument('-n', '--nobj', type=int, default=500, metavar='',
                        help='number of objects to simulate (required input)')
    parser.add_argument('-rs', '--rowstart', type=int, default=0, metavar='',
                        help='zero indexed, row of ra,dec,mags table, after it is cut to brick, to start on')
    parser.add_argument('--do_skipids', type=str, choices=['no','yes'],default='no', help='inject skipped ids for brick, otherwise run as usual')
    parser.add_argument('--do_more', type=str, choices=['no','yes'],default='no', help='yes if running more randoms b/c TS returns too few targets')
    parser.add_argument('--minid', type=int, default=None, help='set if do_more==yes, minimum id to consider, useful if adding more randoms mid-run')
    parser.add_argument('--randoms_db', default='obiwan_elg', help='desi db table name for randoms')
    parser.add_argument('--randoms_from_fits', default=None, help='set to read randoms from fits file instead of scidb2.nersc.gov db, set to absolute path of local fits file on computer')
    parser.add_argument('--dont_sort_sampleid', action="store_true", default=False, help='False to sort sample by id')
    parser.add_argument('-t', '--threads', type=int, default=1, metavar='',
                        help='number of threads to use when calling The Tractor')
    parser.add_argument('-z', '--zoom', nargs=4, default=(0, 3600, 0, 3600), type=int, metavar='',
                        help='see runbrick.py; (default is 0 3600 0 3600)')
    parser.add_argument('-survey-dir', '--survey_dir', metavar='',
                        help='Location of survey-ccds*.fits.gz')
    parser.add_argument('--add_sim_noise', action="store_true", help="set to add noise to simulated sources")
    parser.add_argument('-testA','--image_eq_model', action="store_true", help="set to set image,inverr by model only (ignore real image,invvar)")
    parser.add_argument('--all-blobs', action='store_true',
                        help='Process all the blobs, not just those that contain simulated sources.')
    parser.add_argument('--stage', choices=['tims', 'image_coadds', 'srcs', 'fitblobs', 'coadds','writecat'],
                        type=str, default=None, metavar='', help='Run through the stage then stop')
    parser.add_argument('--no_cleanup', action='store_true',default=False,
                        help='useful for test_checkpoint function')
    parser.add_argument('--early_coadds', action='store_true',default=False,
                        help='add this option to make the JPGs before detection/model fitting')
    parser.add_argument('--bricklist',action='store',default='bricks-eboss-ngc.txt',\
                        help='if using mpi4py, $LEGACY_SURVEY_DIR/bricklist')
    parser.add_argument('--nproc', type=int,action='store',default=1,\
                        help='if using mpi4py')
    parser.add_argument('--all_blobs', action='store_true',default=False,
                        help='fit models to all blobs, not just those containing sim sources')
    parser.add_argument('--subset', type=int, default=0,
                        help='COSMOS subset number [0 to 4, 10 to 12], only used if dataset = cosmos')
    #parser.add_argument('--checkpoint', action='store_true',default=False,
    #                    help='turn on checkpointing')
    parser.add_argument('--skip_ccd_cuts', action='store_true',default=False,
                        help='no ccd cuts')
    parser.add_argument('--overwrite_if_exists', action='store_true',default=False,
                        help='run the code even if expected output already exists')
    parser.add_argument('-v', '--verbose', action='store_true', help='toggle on verbose output')
    parser.add_argument('--ps',default=None,help = 'Not sure what it is now...#TODO')
    parser.add_argument('--pickle',dest='pickle_pat',default=None, help = 'intermediate savings')
    parser.add_argument('--checkpoint',dest='checkpoint_filename',default = None, help = 'I dont know either...')
    parser.add_argument('--ps-t0', type=int, default=0, help='Unix-time start for "--ps"')
    parser.add_argument('--write-stage',default='writecat')
    parser.add_argument('--run',default=None, type=str, choices=['north','decam','90prime', 'mosaic'],required=True)
    parser.add_argument('--less-masking', action='store_true',default=False,help='reduce masked star radius')
    parser.add_argument('--no-galaxy-forcepsf', action='store_true',default=False,help='fit_back function??')
    return parser

def create_metadata(kwargs=None):
    """fits_table with configuration-like params for the simulated sources

    TODO: Should metacat table have a rowstart column?
    TODO: One metacat table per brick, instead of one per `rs*` directory?

    Args:
        kwargs: configuration-like params for the simulated sources
            {'brickname': which chunk of sky
            'objtype': star,elg,lrg,qso
            'nobj': number of simulated sources for this run
            }

    Returns:
        Nothing
        writes the 'metacat' fits_table to disk and stores it
        in the kwargs input arg
    """
    assert(kwargs is not None)
    log = logging.getLogger('decals_sim')
    # Pack the input parameters into a meta-data table and write out.
    #metacols = [
    #    ('BRICKNAME', 'S10'),
    #    ('OBJTYPE', 'S10'),
    #    ('NOBJ', 'i4'),
    #    ('CHUNKSIZE', 'i2'),
    #    ('NCHUNK', 'i2'),
    #    ('ZOOM', 'i4', (4,)),
    #    ('SEED', 'S20'),
    #    ('RMAG_RANGE', 'f4', (2,))]
    #metacat = Table(np.zeros(1, dtype=metacols))
    metacat = fits_table()
    for key in ['brickname','objtype']: #,'nchunk']:
        metacat.set(key, np.array( [kwargs[key]] ))
    metacat.set('nobj', np.array( [kwargs['args'].nobj] ))
    metacat.set('zoom', np.array( [kwargs['args'].zoom] ))
    #metacat['RMAG_RANGE'] = kwargs['args'].rmag_range
    #if not kwargs['args'].seed:
    #    log.info('Random seed = {}'.format(kwargs['args'].seed))
    #    metacat['SEED'] = kwargs['args'].seed
    #metacat_dir = os.path.join(kwargs['decals_sim_dir'], kwargs['objtype'],kwargs['brickname'][:3],kwargs['brickname'])
    metacat_dir= get_outdir_runbrick(kwargs['decals_sim_dir'],
                             kwargs['brickname'],kwargs['rowst'],
                             do_skipids=kwargs['do_skipids'],do_more=kwargs['do_more'])
    if not os.path.exists(metacat_dir):
        os.makedirs(metacat_dir)
    metafile = os.path.join(metacat_dir, 'metacat'+get_fnsuffix(**kwargs))
    log.info('Writing {}'.format(metafile))
    if os.path.isfile(metafile):
        os.remove(metafile)
    metacat.writeto(metafile)
    # Store new stuff
    kwargs['metacat']=metacat
    kwargs['metacat_dir']=metacat_dir


def create_ith_simcat(d=None):
    """Write 'simcat' and 'skipped_ids' tables for a given sample of sources

    Args:
        d: {'Samp': fits_table for the properties of sources in the brick
            'brickwcs': WCS object for the brick
            'metacat': fits_table with configuration params for the simulated sources
            }

    Returns:
        Nothing, saves the 'simcat' and 'skipped_ids' tables
        Adds 'simcat' table to dict 'd'
    """
    assert(d is not None)
    log = logging.getLogger('decals_sim')
    #chunksuffix = '{:02d}'.format(ith_chunk)
    # Build and write out the simulated object catalog.
    #seed= d['seeds'][ith_chunk]
    #simcat = build_simcat(d['nobj'], d['brickname'], d['brickwcs'], d['metacat'], seed)
    simcat, skipped_ids = build_simcat(Samp=d['Samp'],brickwcs=d['brickwcs'],meta=d['metacat'])
    # Simcat
    simcat_dir = get_outdir_runbrick(d['decals_sim_dir'],
                           d['brickname'],d['rowst'],
                           do_skipids=d['do_skipids'],do_more=d['do_more'])
    if not os.path.exists(simcat_dir):
        os.makedirs(simcat_dir)
    #simcatfile = os.path.join(simcat_dir, 'simcat-{}-{}-row{}-{}.fits'.format(d['brickname'], d['objtype'],rowstart,rowend)) # chunksuffix))
    simcatfile = os.path.join(simcat_dir, 'simcat'+get_fnsuffix(**d))
    if os.path.isfile(simcatfile):
        os.remove(simcatfile)
    simcat.writeto(simcatfile)
    log.info('Wrote {}'.format(simcatfile))
    # Skipped Ids
    if len(skipped_ids) > 0:
        skip_table= fits_table()
        skip_table.set('ids',skipped_ids)
        name= os.path.join(simcat_dir,'skippedids'+get_fnsuffix(**d))
        if os.path.exists(name):
            os.remove(name)
            log.info('Removed %s' % name)
        skip_table.writeto(name)
        log.info('HUI-TEST:::stuck here?')
        log.info('Wrote {}'.format(name))
    # add to dict
    log.info('HUI-TEST:::or stuck here?')
    print('add to dict')
    d['simcat']= simcat
    d['simcat_dir']= simcat_dir
    log.info('HUI-TEST:::or exiting?')

def get_checkpoint_fn(outdir,brick,rowstart):
    return os.path.join(outdir,'checkpoint',
                        brick[:3],brick,
                        'checkpoint_rs%d.pickle' % rowstart)

def get_runbrick_setup(**kwargs):
    """Convert runbrick.py cmd line options into `**kwargs` for run_brick()

    The command line options depend on the Data Release (e.g. the
        legacypipe code version. The cmd line options associated with
        each DR get modified and repackaged into a dict in
        legacypipe.runbrick so this converter is required to call run_brick
        appropriately

    Args:
        **kwargs: dict of the cmd line options to obiwan.kenobi.py

    Returns:
        dict to use when calling legacypipe.runbrick.run_brick like
            run_brick(brickname, survey, `**dict`)
    """
    dataset= kwargs['dataset']
    assert(dataset in DATASETS)
    from legacypipe.runbrick import get_runbrick_kwargs
    from legacypipe.runbrick import get_parser as get_runbrick_parser
    zm= kwargs['zoom']
    cmd_line= ['--skip','--skip-calibs',
           '--zoom','%d' % zm[0],'%d' % zm[1],'%d' % zm[2],'%d' % zm[3],
           '--no-unwise-coadds', '--threads','%d' % kwargs['threads'],'--pickle',kwargs['pickle_pat'],'--run',kwargs['run']]
    if kwargs['checkpoint_filename'] is not None:
        checkpoint_fn = kwargs['checkpoint_filename']
        #get_checkpoint_fn(kwargs['outdir'],kwargs['brick'], kwargs['rowstart'])
        cmd_line += ['--checkpoint',checkpoint_fn]
    if kwargs['stage']:
        cmd_line += ['--stage', kwargs['stage']]
    if kwargs['early_coadds']:
        #boolean; generate the early coadds?
        cmd_line += ['--early-coadds', '--stage', 'image_coadds']
    if kwargs['skip_ccd_cuts']:
        cmd_line += ['--skip_ccd_cuts']
    if kwargs['no_galaxy_forcepsf']:
        cmd_line += ['--no-galaxy-forcepsf']
    if kwargs['less_masking']:
        cmd_line += ['--less-masking']
    if kwargs['write_stage']:
        cmd_line += ['--write-stage',kwargs['write_stage']]


    rb_parser= get_runbrick_parser()
    rb_opt = rb_parser.parse_args(args=cmd_line)
    rb_optdict = vars(rb_opt)
    # remove keys as Dustin' does
    _= rb_optdict.pop('ps', None)
    _= rb_optdict.pop('verbose',None)
    _, rb_kwargs= get_runbrick_kwargs(**rb_optdict)
    return rb_kwargs

def do_one_chunk(d=None):
    """Runs the legacypipe/Tractor pipeline on images with simulated sources

    Args:
        d: {'args': obiwan.kenobi.py cmd line argparse.Namespace object
            'brickname': chunk of sky
            'metacat': fits_table configuration params for the simulated sources
            'simcat': fits_table simulated source catalog for a given brick (not CCD).

    Note:
        run_brick() is 'main' for the legacypipe/Tractor pipeline

    Returns:
        Nothing, but this func end ups writing out all the obiwan results
    """
    log = logging.getLogger('decals_sim')
    assert(d is not None)
    kw= dict(dataset=d['args'].dataset,\
             survey_dir=d['survey_dir'], \
             metacat=d['metacat'], simcat=d['simcat'], \
             output_dir=d['simcat_dir'], \
             add_sim_noise=d['args'].add_sim_noise, seed=d['seed'],\
             image_eq_model=d['args'].image_eq_model)
    
    if d['args'].dataset == 'cosmos':
        kw.update(subset=d['args'].subset)
        simdecals= SimDecalsCosmos(**kw)
    else:
        simdecals = SimDecals(**kw)
    # Use Tractor to just process the blobs containing the simulated sources.
    if d['args'].all_blobs:
        blobxy = None
    else:
        blobxy = zip(d['simcat'].get('x'), d['simcat'].get('y'))
    # Default runbrick call sequence
    obiwan_kwargs= vars(d['args'])
    runbrick_kwargs= get_runbrick_setup(**obiwan_kwargs)
    # Obiwan modifications
    runbrick_kwargs.update(blobxy=blobxy)
    #plotbase='obiwan')
    log.info('Calling run_brick with: ')
    log.info('brickname= %s' % d['brickname'])
#    log.info('simdecals= ',simdecals)
    log.info('runbrick_kwargs= ',runbrick_kwargs)
    # Run it: run_brick(brick, survey obj, **kwargs)
    np.random.seed(d['seed'])
    log.info(runbrick_kwargs)
    run_brick(d['brickname'], simdecals, **runbrick_kwargs)

def dobash(cmd):
    print('UNIX cmd: %s' % cmd)
    if os.system(cmd): raise ValueError

def do_ith_cleanup(d=None):
    """Moves all obiwan+legacypipe outputs to a new directory stucture

    Uses rsync to move everthing, if all the rsync's succeed then all the
        original files and directories are removed

    Args:
        d: dict with keys brickname, simcat_dir
    """
    assert(d is not None)
    log = logging.getLogger('decals_sim')
    log.info('Cleaning up...')
    brick= d['brickname']
    bri= brick[:3]
    outdir= d['simcat_dir']
    rsdir= os.path.basename(outdir)
    # outdir/obj
    base= os.path.dirname(
                os.path.dirname(
                    os.path.dirname(outdir)))

    drs= ['obiwan','coadd']
    print(d)
    print(d['args'])
    if not d['args'].early_coadds:
        drs += ['metrics','tractor','tractor-i']
    for dr in drs:
        dobash('mkdir -p %s/%s/%s/%s/%s' % \
                (base,dr,bri,brick,rsdir))

    # obiwan
    dobash('mv %s/*.fits %s/obiwan/%s/%s/%s/' % \
            (outdir,  base,bri,brick,rsdir))
    dobash('mv %s/coadd/%s/%s/%s/sim_ids_added.fits %s/obiwan/%s/%s/%s/' % \
            (base,bri,brick,rsdir,  base,bri,brick,rsdir))
    # coadd
    #dobash('mv %s/coadd/%s/%s/* %s/coadd/%s/%s/%s/' % \
    #         (outdir,bri,brick,  base,bri,brick,rsdir))

    if not d['args'].early_coadds:
        # metrics,tractor,tractor-i
        for dr in ['metrics','tractor','tractor-i']:
            dobash('mv %s/%s/%s/* %s/%s/%s/%s/%s/' % \
                 (outdir,dr,bri,  base,dr,bri,brick,rsdir))
    # Remove original outdir
    dobash('rm -r %s' % outdir)

    # Remove unneeded coadd files
    names= ['nexp','depth']
    if not d['args'].early_coadds:
        drs+= ['chi2']
    for name in names:
        dobash('rm %s/coadd/%s/%s/%s/*%s*' %
                    (base,bri,brick,rsdir,name))
    if rsdir != 'rs0':
        # jpgs are nice to look at, but only keep in 1 dir
        for name in ['jpg']:
            dobash('rm %s/coadd/%s/%s/%s/*.%s' %
                        (base,bri,brick,rsdir,name))
    #remove the pickles, I don't know the argument to remmove them, so I do it manually...
    dobash("rm %s/pickles/%s/%s/%s/runbrick-%s*"%(base,bri,brick,rsdir,brick))


def get_sample(objtype,brick,randoms_db,
               minid=None,randoms_from_fits='',
               do_skipids='no',outdir=None,
               dont_sort_sampleid=False):
    """Gets all simulated randoms for a brick from PSQl db, and applies all relevant cuts

    Args:
        objtype: elg,lrg
        brick:
        randoms_db: name of PSQL db for randoms, e.g. obiwan_elg_ra175
        minid: None, unless do_more == yes then it is an integer for the randoms id to start from
        randoms_from_fits: None or filename of fits_table to use for randoms
        do_skipids: yes or no, rerunning on all skipped randoms?
        outdir: None if do_skipids='no'; otherwise path like $CSCRATCH/obiwan_out/elg_9deg2_ra175
        dont_sort_sampleid: False to sort sample by id


    Returns:
        tupe: sample fits_table, seed
    """
    assert(do_skipids in ['yes','no'])
    if do_skipids == 'yes':
        assert(not outdir is None)
    if randoms_from_fits:
        Samp,seed= fits_table(randoms_from_fits),1
    else:
      if do_skipids == 'no':
        Samp,seed= getSrcsInBrick(brick,objtype, db_table=randoms_db)
      elif do_skipids == 'yes':
        skip_ids= get_skip_ids(outdir, brick, objtype)
        Samp,seed= getSrcsInBrick(brick,objtype, db_table=randoms_db,
                             skipped_ids= skip_ids)
    # Already did these cuts in decals_sim_radeccolors
    #r0,r1,d0,d1= brickwcs.radec_bounds()
    #Samp.cut( (Samp.ra >= r0)*(Samp.ra <= r1)*\
    #          (Samp.dec >= d0)*(Samp.dec <= d1) )
    # Sort by Sersic n low -> high (if elg or lrg)
    # Apply cuts
    if minid:
      Samp.cut( Samp.id >= minid )
    if dont_sort_sampleid == False:
        # breaks clustering but robus to adding more ids
        Samp= Samp[np.argsort(Samp.id) ]
    return Samp,seed



def main(args=None):
    """Main routine which parses the optional inputs."""
    t0= Time()
    # Command line options
    if args is None:
        # Read from cmd line
        parser= get_parser()
        args = parser.parse_args(args=args)
    else:
        # args is already a argparse.Namespace obj
        pass
    # Print calling sequence
    print('Args:', args)
    if args.do_more == 'yes':
      assert(not args.minid is None)
    # Setup loggers
    if args.verbose:
        lvl = logging.DEBUG
    else:
        lvl = logging.INFO
    logging.basicConfig(level=lvl, stream=sys.stdout) #,format='%(message)s')
    log = logging.getLogger('decals_sim')
    # Sort through args
    #log.info('decals_sim.py args={}'.format(args))
    #max_nobj=500
    #max_nchunk=1000
    #if args.ith_chunk is not None: assert(args.ith_chunk <= max_nchunk-1)
    #assert(args.nchunk <= max_nchunk)
    #assert(args.nobj <= max_nobj)
    #if args.ith_chunk is not None:
    #    assert(args.nchunk == 1) #if choose a chunk, only doing 1 chunk
    if args.nobj is None:
        parser.print_help()
        sys.exit(1)

    # Exit if expected output already exists
    rsdir= get_outdir_runbrick(args.outdir,
                        args.brick,args.rowstart,
                        do_skipids=args.do_skipids,
                        do_more=args.do_more)
    rsdir= os.path.basename(rsdir)
    tractor_fn= os.path.join(args.outdir,
                    'tractor',args.brick[:3],args.brick,
                    rsdir,
                    'tractor-%s.fits' % args.brick)
    if (os.path.exists(tractor_fn) &
        (not args.overwrite_if_exists)):
       print('Exiting, already finished %s' % tractor_fn)
       return 0 #sys.exit(0)
    #print(stamp_stat_fn)
    #f = open(stamp_stat_fn,'w')
    #f.close()
    brickname = args.brick
    objtype = args.objtype
    #jun7
    global BRICKNAME
    BRICKNAME = args.brick
    global SURVEY_DIR
    SURVEY_DIR = args.survey_dir
    # Output dir
    decals_sim_dir = args.outdir

    #nchunk = args.nchunk
    #rand = np.random.RandomState(args.seed) # determines seed for all chunks
    #seeds = rand.random_integers(0,2**18, max_nchunk)

    log.info('Object type = {}'.format(objtype))
    #log.info('Number of objects = {}'.format(nobj))
    #log.info('Number of chunks = {}'.format(nchunk))
    # Optionally zoom into a portion of the brick
    survey = LegacySurveyData(survey_dir=args.survey_dir)
    brickinfo= get_brickinfo_hack(survey,brickname)
    #brickinfo = survey.get_brick_by_name(brickname)
    #print(brickname)
    brickwcs = wcs_for_brick(brickinfo)
    W, H, pixscale = brickwcs.get_width(), brickwcs.get_height(), brickwcs.pixel_scale()

    log.info('Brick = {}'.format(brickname))
    if args.zoom is not None: # See also runbrick.stage_tims()
        (x0, x1, y0, y1) = args.zoom
        W = x1 - x0
        H = y1 - y0
        brickwcs = brickwcs.get_subimage(x0, y0, W, H)
        log.info('Zoom (pixel boundaries) = {}'.format(args.zoom))
    targetrd = np.array([brickwcs.pixelxy2radec(x, y) for x, y in
                         [(1,1), (W,1), (W,H), (1,H), (1,1)]])

    radec_center = brickwcs.radec_center()
    log.info('RA, Dec center = {}'.format(radec_center))
    log.info('Brick = {}'.format(brickname))
    t0= ptime('First part of Main()',t0)

    # SAMPLE table
    sample_kwargs= {"objtype":args.objtype,
                    "brick":args.brick,
                    "outdir":args.outdir,
                    "randoms_db":args.randoms_db,
                    "minid":args.minid,
                    "do_skipids":args.do_skipids,
                    "randoms_from_fits":args.randoms_from_fits,
                    "dont_sort_sampleid":args.dont_sort_sampleid}
    Samp,seed= get_sample(**sample_kwargs)
    
    #MS star compiling
    try:
       Samp= Samp[args.rowstart:args.rowstart + args.nobj]
    except:
       Samp= Samp
    # Performance
    #if objtype in ['elg','lrg']:
    #    Samp=Samp[np.argsort( Samp.get('%s_n' % objtype) )]
    print('Max sample size=%d, actual sample size=%d' % (args.nobj,len(Samp)))
    assert(len(Samp) <= args.nobj)
    t0= ptime('Got randoms sample',t0)

    # Store args in dict for easy func passing
    kwargs=dict(Samp=Samp,\
                brickname=brickname, \
                checkpoint_filename=args.checkpoint_filename, \
                seed= seed,
                decals_sim_dir= decals_sim_dir,\
                brickwcs= brickwcs, \
                objtype=objtype,\
                nobj=len(Samp),\
                maxobjs=args.nobj,\
                rowst=args.rowstart,\
                do_skipids=args.do_skipids,\
                do_more=args.do_more,\
                minid=args.minid,\
                survey_dir=args.survey_dir,\
                args=args)

    # Stop if starting row exceeds length of radec,color table
    if len(Samp) == 0:
        fn= get_outdir_runbrick(kwargs['decals_sim_dir'],
                        kwargs['brickname'],kwargs['rowst'],
                        do_skipids=kwargs['do_skipids'],do_more=kwargs['do_more'])
        fn+= '_exceeded.txt'
        junk= os.system('touch %s' % fn)
        print('Wrote %s' % fn)
        raise ValueError('starting row=%d exceeds number of artificial sources, quit' % args.rowstart)

    # Create simulated catalogues and run Tractor
    create_metadata(kwargs=kwargs)
    t0= ptime('create_metadata',t0)
    # do chunks
    #for ith_chunk in chunk_list:
    #log.info('Working on chunk {:02d}/{:02d}'.format(ith_chunk,kwargs['nchunk']-1))
    # Random ra,dec and source properties
    create_ith_simcat(d=kwargs)
    t0= ptime('create_ith_simcat',t0)
    # Run tractor
    do_one_chunk(d=kwargs)
    t0= ptime('do_one_chunk',t0)
    # Clean up output
    if args.no_cleanup == False:
        do_ith_cleanup(d=kwargs)
    t0= ptime('do_ith_cleanup',t0)
    log.info('All done!')
    return 0

if __name__ == '__main__':
    print('obiwan started at %s' % time_builtin.strftime("%Y-%m-%d %H:%M:%S"))
    start_time = time_builtin.time()
    main()
    end_time = time_builtin.time()
    print('obiwan finishes in %f hours' %((end_time-start_time)/3600.))
    print('obiwan finshed at %s' % time_builtin.strftime("%Y-%m-%d %H:%M:%S"))
