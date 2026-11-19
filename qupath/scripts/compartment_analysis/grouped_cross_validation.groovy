// Subject folds and scores are compiled; stage callbacks own each independent fit.
import org.mif.classification.GroupedCrossValidation
return [run: {List entries,List classes,List candidates,int folds,int seed,Closure fit,Closure predict,Closure close ->
    GroupedCrossValidation.run(entries,classes,candidates,folds,seed,
        [fit: {Map candidate,List training,int randomSeed -> fit.call(candidate,training,randomSeed)},
         predict: {Map model,List held -> predict.call(model,held)},
         close: {Map model -> close.call(model)}] as GroupedCrossValidation.Operations)
}]
