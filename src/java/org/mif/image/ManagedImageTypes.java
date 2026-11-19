package org.mif.image;

import qupath.lib.images.ImageData;
import qupath.lib.images.ImageData.ImageType;

/** Initializes QuPath image types from the pipeline's reviewed image roles. */
public final class ManagedImageTypes {
    private ManagedImageTypes() {}

    /**
     * Supplies a type only when the saved image has no explicit selection.
     *
     * We use the entry's validated role and paired-image modality, rather than
     * guessing from its filename or channel count. Existing image types and
     * stain choices remain unchanged. The caller saves an initialized image
     * through its ordinary project write and saved-state attestation.
     *
     * @param imageData image being created or upserted in the selected project
     * @param imageRole primary_mif or moving_reference from the entry request
     * @param referenceModality reviewed paired-image modality, absent for mIF
     * @return whether the unset image was initialized and needs saving
     */
    public static boolean initializeIfUnset(
            ImageData<?> imageData, String imageRole, String referenceModality) {
        if (imageData == null)
            throw new IllegalArgumentException("Image type initialization requires image data");
        if (imageData.getImageType() != ImageType.UNSET)
            return false;
        ImageType selected;
        if ("primary_mif".equals(imageRole)) {
            selected = ImageType.FLUORESCENCE;
        } else if ("moving_reference".equals(imageRole)) {
            if (referenceModality == null || referenceModality.isBlank())
                throw new IllegalArgumentException("Paired image type requires its reviewed modality");
            selected = switch (referenceModality) {
                case "he" -> ImageType.BRIGHTFIELD_H_E;
                case "brightfield" -> ImageType.BRIGHTFIELD_OTHER;
                case "fluorescence" -> ImageType.FLUORESCENCE;
                default -> ImageType.OTHER;
            };
        } else {
            throw new IllegalArgumentException("Image type requires a managed image role");
        }
        imageData.setImageType(selected);
        return true;
    }
}
