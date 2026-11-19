package org.mif.image;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerMetadata;
import qupath.lib.regions.RegionRequest;

/** The accepted v2 cycle recipe, shared across every channel of each original image. */
final class CycleRegistrationGeometry {
    private static final double EPS = 1e-7;
    final int reference, x, y, width, height;
    private final double[][] inverse;

    CycleRegistrationGeometry(JsonObject root, List<ImageServerMetadata> headers) throws IOException {
        JsonObject value = root.getAsJsonObject("registration");
        fields(value, "schema_name", "schema_version", "reference_member", "registration_channels", "shared_stain",
                "grids", "transforms", "crop", "coordinate_convention", "transform_direction", "interpolation", "output_dtype",
                "reviewed", "output_coordinates", "output_origin_reference_um", "moving_to_output_um");
        if (!"mif-accepted-cycle-registration".equals(value.get("schema_name").getAsString()) || integer(value, "schema_version") != 1
                || !"integer_pixel_centres".equals(value.get("coordinate_convention").getAsString())
                || !"moving_to_reference_um".equals(value.get("transform_direction").getAsString())
                || !"linear".equals(value.get("interpolation").getAsString())
                || !"float32".equals(value.get("output_dtype").getAsString())
                || !value.get("reviewed").isJsonPrimitive() || !value.get("reviewed").getAsJsonPrimitive().isBoolean()
                || !value.get("reviewed").getAsBoolean()
                || !"crop_local_zero_origin".equals(value.get("output_coordinates").getAsString()))
            throw new IOException("Unsupported accepted cycle registration");
        String stain = value.get("shared_stain").getAsString();
        if (stain.isBlank() || !stain.equals(stain.strip()) || stain.length() > 200 || stain.codePoints().anyMatch(Character::isISOControl))
            throw new IOException("Confirm the actual corresponding stain for all cycles");
        reference = integer(value, "reference_member");
        if (reference >= headers.size()) throw new IOException("The reference cycle is unavailable");
        JsonArray crop = array(value, "crop", 4);
        x = index(crop, 0); y = index(crop, 1); width = index(crop, 2); height = index(crop, 3);
        if (width < 1 || height < 1) throw new IOException("The common cycle crop is empty");
        JsonArray grids = array(value, "grids", headers.size());
        JsonArray transforms = array(value, "transforms", headers.size());
        JsonArray outputTransforms = array(value, "moving_to_output_um", headers.size());
        JsonArray channels = array(value, "registration_channels", headers.size());
        JsonArray origin = array(value, "output_origin_reference_um", 2);
        var fixed = headers.get(reference).getPixelCalibration();
        double fx = fixed.getPixelWidthMicrons(), fy = fixed.getPixelHeightMicrons();
        equal(number(origin, 0), x * fx); equal(number(origin, 1), y * fy);
        inverse = new double[headers.size()][6];
        for (int m = 0; m < headers.size(); m++) {
            var header = headers.get(m);
            var pixel = header.getPixelCalibration();
            JsonObject grid = grids.get(m).getAsJsonObject();
            fields(grid, "width", "height", "x_um", "y_um");
            if (integer(grid, "width") != header.getWidth() || integer(grid, "height") != header.getHeight()
                    || header.getWidth() < 2 || header.getHeight() < 2 || index(channels, m) >= header.getSizeC())
                throw new IOException("An original cycle grid or corresponding-stain channel changed");
            double mx = number(grid, "x_um"), my = number(grid, "y_um");
            if (mx <= 0 || my <= 0) throw new IOException("Cycle pixel calibration must be positive");
            equal(mx, pixel.getPixelWidthMicrons()); equal(my, pixel.getPixelHeightMicrons());
            JsonArray matrix = transforms.get(m).getAsJsonArray();
            JsonArray out = outputTransforms.get(m).getAsJsonArray();
            if (matrix.size() != 6 || out.size() != 6) throw new IOException("A physical cycle matrix needs six values");
            double a = number(matrix, 0), b = number(matrix, 1), tx = number(matrix, 2);
            double c = number(matrix, 3), d = number(matrix, 4), ty = number(matrix, 5), determinant = a*d-b*c;
            if (Math.abs(a*a+c*c-1) > 1e-6 || Math.abs(b*b+d*d-1) > 1e-6 || Math.abs(a*b+c*d) > 1e-6
                    || Math.abs(determinant-1) > 1e-6)
                throw new IOException("Cycle registration requires a physical rigid transform without scale, shear or reflection");
            if (m == reference && (a != 1 || b != 0 || tx != 0 || c != 0 || d != 1 || ty != 0))
                throw new IOException("The reference cycle must retain the identity transform");
            double[] expected = {a,b,tx-x*fx,c,d,ty-y*fy};
            for (int i = 0; i < 6; i++) equal(number(out, i), expected[i]);
            inverse[m] = new double[]{d*fx/determinant/mx, -b*fy/determinant/mx, (b*ty-d*tx)/determinant/mx,
                    -c*fx/determinant/my, a*fy/determinant/my, (c*tx-a*ty)/determinant/my};
            for (double xx : new double[]{x, x+(double)width-1}) for (double yy : new double[]{y, y+(double)height-1}) {
                double[] q = inverse[m];
                double sx = q[0]*xx+q[1]*yy+q[2], sy = q[3]*xx+q[4]*yy+q[5];
                if (sx < -EPS || sy < -EPS || sx > header.getWidth()-1+EPS || sy > header.getHeight()-1+EPS)
                    throw new IOException("The common crop includes unsupported interpolation footprints");
            }
        }
        int[] cycles = new int[headers.size()]; java.util.Arrays.fill(cycles, -1);
        for (var item : root.getAsJsonArray("channels")) {
            JsonObject channel = item.getAsJsonObject();
            int member = integer(channel, "member_index"), cycle = integer(channel, "imaging_cycle");
            if (cycles[member] != -1 && cycles[member] != cycle)
                throw new IOException("Register one stitched image per distinct imaging cycle");
            cycles[member] = cycle;
        }
        Set<Integer> unique = new HashSet<>();
        for (int cycle : cycles) if (!unique.add(cycle)) throw new IOException("Register one stitched image per distinct imaging cycle");
    }

    /** Linear samples from original pixel regions, with bounded source rasters. */
    float[][] read(ImageServer<BufferedImage> source, int member, RegionRequest region, int width, int height) throws IOException {
        float[][] result = new float[source.nChannels()][Math.multiplyExact(width, height)];
        double downsample = 1;
        // Reduced previews may use the source's stored pyramid. Level zero remains
        // the exact quantitative contract and never passes through a preview image.
        double[] q = inverse[member];
        double desired = region.getDownsample() * Math.min(Math.hypot(q[0], q[3]), Math.hypot(q[1], q[4]));
        for (var level : source.getMetadata().getLevels())
            if (level.getDownsample() <= desired) downsample = Math.max(downsample, level.getDownsample());
        readBlock(source, member, region, width, result, downsample, 0, 0, width, height);
        return result;
    }

    private void readBlock(ImageServer<BufferedImage> source, int member, RegionRequest region, int outputWidth,
            float[][] result, double sourceDownsample, int ox, int oy, int width, int height) throws IOException {
        double[] q = inverse[member];
        double step = region.getDownsample();
        double minX = Double.POSITIVE_INFINITY, minY = minX, maxX = Double.NEGATIVE_INFINITY, maxY = maxX;
        for (int dx : new int[]{ox, ox+width-1}) for (int dy : new int[]{oy, oy+height-1}) {
            double fx = x+region.getX()+dx*step, fy = y+region.getY()+dy*step;
            double sx = snap(q[0]*fx+q[1]*fy+q[2]), sy = snap(q[3]*fx+q[4]*fy+q[5]);
            minX = Math.min(minX,sx); maxX = Math.max(maxX,sx); minY = Math.min(minY,sy); maxY = Math.max(maxY,sy);
        }
        if (minX < 0 || minY < 0 || maxX > source.getWidth()-1 || maxY > source.getHeight()-1)
            throw new IOException("A retained cycle pixel lacks complete interpolation support");
        int left = Math.max(0,(int)Math.floor(minX)), top = Math.max(0,(int)Math.floor(minY));
        int right = Math.min(source.getWidth()-1,(int)Math.ceil(maxX+sourceDownsample-1));
        int bottom = Math.min(source.getHeight()-1,(int)Math.ceil(maxY+sourceDownsample-1));
        long samples = (long)Math.ceil((right-left+1)/sourceDownsample) * (long)Math.ceil((bottom-top+1)/sourceDownsample) * source.nChannels();
        if (samples > 4_194_304L) {
            if (width >= height && width > 1) {
                int split = width/2;
                readBlock(source,member,region,outputWidth,result,sourceDownsample,ox,oy,split,height);
                readBlock(source,member,region,outputWidth,result,sourceDownsample,ox+split,oy,width-split,height);
            } else if (height > 1) {
                int split = height/2;
                readBlock(source,member,region,outputWidth,result,sourceDownsample,ox,oy,width,split);
                readBlock(source,member,region,outputWidth,result,sourceDownsample,ox,oy+split,width,height-split);
            } else throw new IOException("An interpolation footprint exceeds the bounded reader allowance");
            return;
        }
        BufferedImage image = source.readRegion(RegionRequest.createInstance(source.getPath(),sourceDownsample,
                left,top,right-left+1,bottom-top+1));
        if (image == null || image.getRaster().getNumBands() != source.nChannels()) throw new IOException("The cycle decoder returned incompatible pixels");
        if (sourceDownsample == 1 && (image.getWidth() != right-left+1 || image.getHeight() != bottom-top+1))
            throw new IOException("The cycle decoder returned an unexpected original-pixel extent");
        var raster = image.getRaster();
        for (int dy = oy; dy < oy+height; dy++) for (int dx = ox; dx < ox+width; dx++) {
            double fx = x+region.getX()+dx*step, fy = y+region.getY()+dy*step;
            double sx = snap((snap(q[0]*fx+q[1]*fy+q[2])-left)/sourceDownsample);
            double sy = snap((snap(q[3]*fx+q[4]*fy+q[5])-top)/sourceDownsample);
            // At stored-pyramid edges, the last available preview sample is held.
            sx = Math.min(sx,image.getWidth()-1); sy = Math.min(sy,image.getHeight()-1);
            int x0 = (int)Math.floor(sx), y0 = (int)Math.floor(sy), x1 = (int)Math.ceil(sx), y1 = (int)Math.ceil(sy);
            double wx = sx-x0, wy = sy-y0;
            for (int channel = 0; channel < result.length; channel++) {
                double lo = raster.getSampleDouble(x0,y0,channel)*(1-wx)+raster.getSampleDouble(x1,y0,channel)*wx;
                double hi = raster.getSampleDouble(x0,y1,channel)*(1-wx)+raster.getSampleDouble(x1,y1,channel)*wx;
                result[channel][dy*outputWidth+dx] = (float)(lo*(1-wy)+hi*wy);
            }
        }
    }

    private static double snap(double value) { return Math.abs(value-Math.rint(value)) < EPS ? Math.rint(value) : value; }
    private static void equal(double a, double b) throws IOException { if (!Double.isFinite(b) || Math.abs(a-b) > 1e-8) throw new IOException("Registered cycle geometry differs from its original source or crop"); }
    private static double number(JsonObject value, String key) throws IOException { return finite(value.get(key)); }
    private static double number(JsonArray value, int index) throws IOException { return finite(value.get(index)); }
    private static double finite(com.google.gson.JsonElement value) throws IOException {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber() || !Double.isFinite(value.getAsDouble()))
            throw new IOException("Cycle geometry requires finite numerical values");
        return value.getAsDouble();
    }
    private static JsonArray array(JsonObject value, String key, int length) throws IOException {
        if (!value.has(key) || !value.get(key).isJsonArray() || value.getAsJsonArray(key).size() != length) throw new IOException("Incomplete cycle geometry: " + key);
        return value.getAsJsonArray(key);
    }
    private static int index(JsonArray values, int index) throws IOException {
        JsonObject item = new JsonObject(); item.add("index",values.get(index)); return integer(item,"index");
    }
    private static int integer(JsonObject value, String key) throws IOException { return AlignedSourceCollectionServer.integer(value,key); }
    private static void fields(JsonObject value, String... names) throws IOException { AlignedSourceCollectionServer.fields(value,names); }
}
