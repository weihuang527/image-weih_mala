import logging
from gunpowder import BatchFilter, Array
from scipy.ndimage import gaussian_filter
from scipy.ndimage.filters import convolve
from numpy.lib.stride_tricks import as_strided
import numpy as np
import time

logger = logging.getLogger(__name__)

class AddLocalShapeDescriptor(BatchFilter):
    '''Create a local segmentation shape discriptor to each voxel.

    Args:

        segmentation (:class:`ArrayKey`): The array storing the segmentation
            to use.

        descriptor (:class:`ArrayKey`): The array of the shape descriptor to
            generate.

        mask (:class:`ArrayKey`, optional): The array to store a binary mask
            the size of the descriptors. Background voxels, which do not have a
            descriptor, will be set to 0. This can be used as a loss scale
            during training, such that background is ignored.

        sigma (float or tuple of float): The context to consider to compute
            the shape descriptor in world units. This will be the standard
            deviation of a Gaussian kernel or the radius of the sphere.

        mode (string): Either ``gaussian`` or ``sphere``. Specifies how to
            accumulate local statistics: ``gaussian`` uses Gaussian convolution
            to compute a weighed average of statistics inside an object.
            ``sphere`` accumulates values in a sphere.

        downsample (int, optional): Downsample the segmentation mask to extract
            the statistics with the given factore. Default is 1 (no
            downsampling).
    '''

    def __init__(
            self,
            segmentation,
            descriptor,
            mask=None,
            sigma=5.0,
            mode='gaussian',
            downsample=1):

        self.segmentation = segmentation
        self.coords = {}
        self.descriptor = descriptor
        self.mask = mask
        try:
            self.sigma = tuple(sigma)
        except:
            self.sigma = (sigma,)*3
        self.mode = mode
        self.downsample = downsample
        self.voxel_size = None
        self.context = None
        self.skip = False

    def setup(self):

        spec = self.spec[self.segmentation].copy()
        spec.dtype = np.float32

        self.voxel_size = spec.voxel_size
        self.provides(self.descriptor, spec)

        if self.mask:
            self.provides(self.mask, spec.copy())

        if self.mode == 'gaussian':
            self.context = tuple(s*3.0 for s in self.sigma)
        elif self.mode == 'sphere':
            self.context = tuple(self.sigma)
        else:
            raise RuntimeError("Unkown mode %s"%mode)

    def prepare(self, request):

        if self.descriptor in request:

            # increase segmentation ROI to fit Gaussian
            context_roi = request[self.descriptor].roi.grow(
                self.context,
                self.context)
            grown_roi = request[self.segmentation].roi.union(context_roi)
            request[self.segmentation].roi = grown_roi

            del request[self.descriptor]
            self.skip = False

        else:

            self.skip = True

        if self.mask and self.mask in request:
            del request[self.mask]

    def process(self, batch, request):

        if self.skip:
            return

        dims = len(self.voxel_size)

        assert dims == 3, "AddLocalShapeDescriptor only works on 3D arrays."

        segmentation_array = batch.arrays[self.segmentation]

        # get voxel roi of requested descriptors -- this is the only region in
        # which we have to compute the descriptors
        seg_roi = segmentation_array.spec.roi
        descriptor_roi = request[self.descriptor].roi
        voxel_roi_in_seg = (
            seg_roi.intersect(descriptor_roi) -
            seg_roi.get_offset())/self.voxel_size

        descriptor = self.__get_descriptors(
            segmentation_array.data,
            voxel_roi_in_seg)

        # create descriptor array
        descriptor_spec = self.spec[self.descriptor].copy()
        descriptor_spec.roi = request[self.descriptor].roi.copy()
        descriptor_array = Array(descriptor, descriptor_spec)

        # create mask array
        if self.mask and self.mask in request:
            channel_mask = (segmentation_array.crop(descriptor_roi).data!=0).astype(np.float32)
            assert channel_mask.shape[-3:] == descriptor.shape[-3:]
            mask = np.array([channel_mask]*descriptor.shape[0])
            batch.arrays[self.mask] = Array(mask, descriptor_spec.copy())

        # crop segmentation back to original request
        seg_request_roi = request[self.segmentation].roi
        cropped_segmentation_array = segmentation_array.crop(seg_request_roi)

        batch.arrays[self.segmentation] = cropped_segmentation_array
        batch.arrays[self.descriptor] = descriptor_array

    def __get_descriptors(self, segmentation, roi):

        roi_slices = roi.get_bounding_box()

        # prepare full-res descriptor arrays for roi
        descriptors = np.zeros((10,) + roi.get_shape(), dtype=np.float32)

        # get sub-sampled shape, roi, voxel size and sigma
        df = self.downsample
        logger.debug("Downsampling segmentation with factor %f", df)
        sub_shape = tuple(s/df for s in segmentation.shape)
        sub_roi = roi/df
        sub_voxel_size = tuple(v*df for v in self.voxel_size)
        sub_sigma_voxel = tuple(s/v for s, v in zip(self.sigma, sub_voxel_size))
        logger.debug("Downsampled shape: %s", sub_shape)
        logger.debug("Downsampled voxel size: %s", sub_voxel_size)
        logger.debug("Sigma in voxels: %s", sub_sigma_voxel)

        # prepare coords array (reuse if we already have one)
        if (sub_shape, sub_voxel_size) not in self.coords:

            logger.debug("Create meshgrid...")

            self.coords[(sub_shape, sub_voxel_size)] = np.array(
                np.meshgrid(
                    np.arange(0, sub_shape[0]*sub_voxel_size[0], sub_voxel_size[0]),
                    np.arange(0, sub_shape[1]*sub_voxel_size[1], sub_voxel_size[1]),
                    np.arange(0, sub_shape[2]*sub_voxel_size[2], sub_voxel_size[2]),
                    indexing='ij'),
                dtype=np.float32)

        coords = self.coords[(sub_shape, sub_voxel_size)]

        # for all labels inside ROI
        for label in np.unique(segmentation[roi_slices]):

            if label == 0:
                continue

            logger.debug("Creating shape descriptors for label %d", label)

            mask = (segmentation==label).astype(np.float32)
            sub_mask = mask[::df, ::df, ::df]

            sub_count, sub_mean_offset, sub_variance, sub_pearson = self.__get_stats(
                coords,
                sub_mask,
                sub_sigma_voxel,
                sub_roi)

            sub_descriptor = np.concatenate([
                sub_mean_offset,
                sub_variance,
                sub_pearson,
                sub_count[None,:]])

            logger.debug("Upscaling descriptors...")
            start = time.time()
            descriptor = self.__upsample(sub_descriptor, df)
            logger.debug("%f seconds", time.time() - start)

            logger.debug("Accumulating descriptors...")
            start = time.time()
            descriptors += descriptor*mask[roi_slices]
            logger.debug("%f seconds", time.time() - start)

        # normalize stats

        # get max possible mean offset for normalization
        if self.mode == 'gaussian':
            # farthes voxel in context is 3*sigma away, but due to Gaussian
            # weighting, sigma itself is probably a better upper bound
            max_distance = np.array(
                [s for s in self.sigma],
                dtype=np.float32)
        elif self.mode == 'sphere':
            # farthes voxel in context is sigma away, but this is almost
            # impossible to reach as offset -- let's take half sigma
            max_distance = np.array(
                [0.5*s for s in self.sigma],
                dtype=np.float32)

        # mean offsets in [0, 1]
        descriptors[[0, 1, 2]] = descriptors[[0, 1, 2]]/max_distance[:, None, None, None]*0.5 + 0.5
        # pearsons in [0, 1]
        descriptors[[6, 7, 8]] = descriptors[[6, 7, 8]]*0.5 + 0.5
        # reset background to 0
        descriptors[[0, 1, 2, 6, 7, 8]] *= (segmentation[roi_slices] != 0)

        # clip outliers
        np.clip(descriptors, 0.0, 1.0, out=descriptors)

        return descriptors

    def __get_stats(self, coords, mask, sigma_voxel, roi):

        # mask for object
        masked_coords = coords*mask

        # number of inside voxels
        logger.debug("Counting inside voxels...")
        start = time.time()
        count = self.__aggregate(mask, sigma_voxel, self.mode, roi)
        # avoid division by zero
        count[count==0] = 1
        logger.debug("%f seconds", time.time() - start)

        # mean
        logger.debug("Computing mean position of inside voxels...")
        start = time.time()
        mean = np.array([
            self.__aggregate(masked_coords[d], sigma_voxel, self.mode, roi)
            for d in range(3)])
        mean /= count
        logger.debug("%f seconds", time.time() - start)

        logger.debug("Computing offset of mean position...")
        start = time.time()
        mean_offset = mean - coords[(slice(None),) + roi.get_bounding_box()]

        # covariance
        logger.debug("Computing covariance...")
        coords_outer = self.__outer_product(masked_coords)
        covariance = np.array([
            self.__aggregate(coords_outer[d], sigma_voxel, self.mode, roi)
            # remove duplicate entries in covariance
            # 0 1 2
            # 3 4 5
            # 6 7 8
            for d in [0, 4, 8, 1, 2, 5]])
        covariance /= count
        covariance -= self.__outer_product(mean)[[0, 4, 8, 1, 2, 5]]
        logger.debug("%f seconds", time.time() - start)

        # variances of z, y, x coordinates
        variance = covariance[[0, 1, 2]]
        # Pearson coefficients of zy, zx, yx
        pearson = covariance[[3, 4, 5]]

        # normalize Pearson correlation coefficient
        variance[variance<1e-3] = 1e-3 # numerical stability
        pearson[0] /= np.sqrt(variance[0]*variance[1])
        pearson[1] /= np.sqrt(variance[0]*variance[2])
        pearson[2] /= np.sqrt(variance[1]*variance[2])

        # normalize variances to interval [0, 1]
        variance[0] /= self.sigma[0]**2
        variance[1] /= self.sigma[1]**2
        variance[2] /= self.sigma[2]**2

        return count, mean_offset, variance, pearson

    def __make_sphere(self, radius):

        logger.debug("Creating sphere with radius %d...", radius)

        r2 = np.arange(-radius, radius)**2
        dist2 = r2[:, None, None] + r2[:, None] + r2
        return (dist2 <= radius**2).astype(np.float32)

    def __aggregate(self, array, sigma, mode='gaussian', roi=None):

        if roi is None:
            roi_slices = (slice(None),)
        else:
            roi_slices = roi.get_bounding_box()

        if mode == 'gaussian':

            return gaussian_filter(
                array,
                sigma=sigma,
                mode='constant',
                cval=0.0,
                truncate=3.0)[roi_slices]

        elif mode == 'sphere':

            radius = sigma[0]
            for d in range(len(sigma)):
                assert radius == sigma[d], (
                    "For mode 'sphere', only isotropic sigma is allowed.")

            sphere = self.__make_sphere(radius)
            return convolve(
                array,
                sphere,
                mode='constant',
                cval=0.0)[roi_slices]

        else:
            raise RuntimeError("Unknown mode %s"%mode)

    def __outer_product(self, array):
        '''Computes the unique values of the outer products of the first dimension
        of ``array``. If ``array`` has shape ``(k, d, h, w)``, for example, the
        output will be of shape ``(k*(k+1)/2, d, h, w)``.
        '''
        k = array.shape[0]
        outer = np.einsum('i...,j...->ij...', array, array)
        return outer.reshape((k**2,)+array.shape[1:])

    def __upsample(self, array, f):

        shape = array.shape
        stride = array.strides

        view = as_strided(
            array,
            (shape[0], shape[1], f, shape[2], f, shape[3], f),
            (stride[0], stride[1], 0, stride[2], 0, stride[3], 0))

        return view.reshape(shape[0], shape[1]*f, shape[2]*f, shape[3]*f)
