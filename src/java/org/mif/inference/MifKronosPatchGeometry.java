package org.mif.inference;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.locationtech.jts.geom.Envelope;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.GeometryFactory;
import org.locationtech.jts.operation.union.UnaryUnionOp;

/** Exact patch footprints shared by labelled training and viewport prediction. */
public final class MifKronosPatchGeometry {
    private static final GeometryFactory FACTORY=new GeometryFactory();
    private static final int[][] OFFSETS={{0,-1},{0,1},{-1,0},{1,0}};
    private MifKronosPatchGeometry() {}

    public static Geometry footprint(int row,int col,double downsample,int patchSize) {
        if(row<0 || col<0 || !Double.isFinite(downsample) || downsample<1
                || (patchSize!=32 && patchSize!=64 && patchSize!=128 && patchSize!=256))
            throw new IllegalArgumentException("Patch row/column and sampling scale are invalid");
        double side=patchSize*downsample;
        return FACTORY.toGeometry(new Envelope(col*side,(col+1)*side,row*side,(row+1)*side));
    }

    /** Incomplete edge patches stay unclassified; there is no implicit padding. */
    public static boolean admitted(Geometry scope,int width,int height,int row,int col,double downsample,int patchSize) {
        if(scope==null || !scope.isValid() || scope.isEmpty() || width<=0 || height<=0)
            throw new IllegalArgumentException("Patch scope and image dimensions must be valid");
        if(row<0 || col<0)return false;
        Geometry patch=footprint(row,col,downsample,patchSize);
        Envelope bounds=patch.getEnvelopeInternal();
        if(bounds.getMaxX()>width || bounds.getMaxY()>height)return false;
        if(!scope.getEnvelopeInternal().intersects(bounds) || !scope.intersects(patch))return false;
        return scope.intersection(patch).getArea()>0;
    }

    public static List<List<Integer>> neighbors(Geometry scope,int width,int height,int row,int col,double downsample,int patchSize) {
        if(!admitted(scope,width,height,row,col,downsample,patchSize))
            throw new IllegalArgumentException("The requested patch is outside the admitted scope");
        List<List<Integer>> found=new ArrayList<>(4);
        for(int[] offset:OFFSETS) {
            int r=row+offset[0],c=col+offset[1];
            if(admitted(scope,width,height,r,c,downsample,patchSize))found.add(List.of(r,c));
        }
        return Collections.unmodifiableList(found);
    }

    /** A patch has one label only when that class's annotation union covers it. */
    public static Map<String,Object> labelPatch(Geometry patch,List<? extends Map<?,?>> annotations) {
        if(patch==null || !patch.isValid() || patch.getArea()<=0)
            throw new IllegalArgumentException("Training patch must have a valid positive-area footprint");
        TreeMap<String,List<Geometry>> classes=new TreeMap<>();
        TreeMap<String,List<String>> ids=new TreeMap<>();
        var seen=new HashSet<String>();
        for(Map<?,?> row:annotations) {
            if(!(row.get("id") instanceof String id) || !(row.get("label") instanceof String label)
                    || id.trim().isEmpty() || label.trim().isEmpty()
                    || !(row.get("geometry") instanceof Geometry geometry)
                    || !geometry.isValid() || !seen.add(id))
                throw new IllegalArgumentException("Training annotation identities, labels and geometry must be explicit and valid");
            if(!geometry.getEnvelopeInternal().intersects(patch.getEnvelopeInternal())
                    || !geometry.intersects(patch) || geometry.intersection(patch).getArea()<=0)continue;
            classes.computeIfAbsent(label,k->new ArrayList<>()).add(geometry);
            ids.computeIfAbsent(label,k->new ArrayList<>()).add(id);
        }
        String label=null,reason="no_training_label";
        List<String> selected=new ArrayList<>();
        if(classes.size()>1) {
            ids.values().forEach(selected::addAll);
            reason="conflicting_labels";
        } else if(!classes.isEmpty()) {
            String candidate=classes.firstKey();
            boolean covered=UnaryUnionOp.union(classes.get(candidate)).covers(patch);
            label=covered?candidate:null;
            selected.addAll(ids.get(candidate));
            reason=covered?"complete_footprint":"partial_coverage";
        }
        Collections.sort(selected);
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("label",label);result.put("annotation_ids",selected);result.put("reason",reason);
        return result;
    }
}
