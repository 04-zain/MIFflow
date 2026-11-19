import org.bytedeco.opencv.opencv_ml.LogisticRegression
import org.bytedeco.opencv.opencv_ml.SVM
import org.bytedeco.opencv.opencv_ml.StatModel
import org.bytedeco.opencv.opencv_ml.TrainData
import org.mif.classification.MifSupervisedObjectClassifier
import org.mif.classification.OpenCvTraining

/** Decode selected cell-head controls and expose the compiled classifier to stages.
 * The authenticated stage bundle owns these settings. Java owns native fitting,
 * bounded cell application and score calculation; the caller owns each model.
 * Stable compiled subtype registration replaces dynamic Groovy class caching.
 */
MifSupervisedObjectClassifier.ensureRegistered()
return [train: { Map config, int seed, Map fit ->
    String method=config.configuration_schema=='qupath-object-rtrees/v1' ? 'rtrees' :
        config.configuration_schema=='qupath-object-svm/v1' ? 'svm' : 'logistic_regression'
    Map p=config[method]
    TrainData data=fit.train_data as TrainData
    StatModel model=null
    try {
        if(method=='rtrees') {
            model=OpenCvTraining.fitForest(data,new OpenCvTraining.ForestSettings(
                p.max_depth as int,p.min_sample_count as int,p.use_1se_rule as boolean,
                p.active_var_count as int,p.calculate_variable_importance as boolean,
                0,false,p.max_trees as int,p.termination_epsilon as double),seed)
        } else if(method=='svm') {
            model=OpenCvTraining.fitSvm(data,new OpenCvTraining.SvmSettings(
                p.kernel=='LINEAR' ? SVM.LINEAR : SVM.RBF,p.c as double,
                p.kernel=='RBF' ? p.gamma as double : 0d,p.max_iterations as int,
                p.termination_epsilon as double,false),seed)
        } else {
            model=OpenCvTraining.fitLogistic(data,new OpenCvTraining.LogisticSettings(
                p.learning_rate as double,p.iterations as int,
                p.regularization=='L2' ? LogisticRegression.REG_L2 : LogisticRegression.REG_DISABLE,
                p.termination_epsilon as double),seed)
        }
        def classifier=new MifSupervisedObjectClassifier(method,model,fit.extractor,
            fit.ordered_path_classes as List,config.feature_source as Map)
        model=null
        classifier
    } finally {model?.close()}
}, featureSource: { Object classifier ->
    classifier instanceof MifSupervisedObjectClassifier ? classifier.featureSource : null
}, scoreKind: { Object classifier ->
    classifier instanceof MifSupervisedObjectClassifier ? classifier.scoreKind : 'native_tree_vote_fraction'
}, close: { Object classifier ->
    if(classifier instanceof MifSupervisedObjectClassifier)classifier.nativeModel.close()
}]
