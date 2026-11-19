// Researcher-facing saved-model review. Reference labels precede model feedback.
// This viewer never trains on held-out data or publishes an accepted classifier.
import javafx.scene.control.*
import java.util.concurrent.Executors
import qupath.lib.gui.QuPathGUI

final class MifKronosGuiTools implements AutoCloseable {
    /** Local native presentation, matching the accepted prediction panels. */
    static void stylePanel(javafx.scene.Parent layout) {
        Map loadedFontFamilies=[:]
        ["Regular","SemiBold"].each {weight->
            def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
            if(stream!=null)try {
                def font=javafx.scene.text.Font.loadFont(stream,15d)
                if(font!=null)loadedFontFamilies[weight]=font.family
            }finally {stream.close()}
        }
        List preferences=System.getProperty("os.name","").startsWith("Windows") ?
            ["Segoe UI",loadedFontFamilies.Regular ?: "Ubuntu Sans","DejaVu Sans"] :
            [loadedFontFamilies.Regular ?: "Ubuntu Sans","Ubuntu","DejaVu Sans"]
        String family=preferences.find {it==loadedFontFamilies.Regular || javafx.scene.text.Font.families.contains(it)} ?: javafx.scene.text.Font.default.family
        String heading=(family==loadedFontFamilies.Regular && loadedFontFamilies.SemiBold) ?
            loadedFontFamilies.SemiBold : (["${family} SemiBold","${family} Semibold"].find {
                javafx.scene.text.Font.families.contains(it.toString())
            } ?: family)
        layout.style="-fx-font-family:'${family.replace("'","")}';-fx-font-size:15px;"
        String css="""
            .root, .dialog-pane { -fx-background-color:white; -fx-accent:#006b70; -fx-focus-color:#006b70; -fx-faint-focus-color:transparent; }
            .label { -fx-text-fill:#172d35; }
            .mif-title, .mif-section-title, .mif-caption { -fx-font-family:'${heading.replace("'","")}'; -fx-font-weight:600; }
            .mif-title { -fx-font-size:20px; }
            .mif-section-title { -fx-font-size:16px; }
            .mif-caption { -fx-text-fill:#294c56; }
            .mif-help { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .mif-footer, .dialog-pane > .button-bar { -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; }
            .dialog-pane > .header-panel { -fx-background-color:white; }
            .scroll-pane, .scroll-pane > .viewport { -fx-background-color:white; -fx-background-insets:0; -fx-padding:0; }
            .separator .line { -fx-border-color:#dce4e4; -fx-border-width:1 0 0 0; }
            .button, .combo-box { -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3; -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-text-fill:#294148; -fx-padding:8 14; }
            .button:hover, .combo-box:hover { -fx-background-color:#eaf2ef; -fx-border-color:#8eaaa5; }
            .button:focused, .combo-box:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 13; }
            .button:pressed { -fx-background-color:#dcece6; }
            .combo-box > .list-cell { -fx-background-color:transparent; -fx-text-fill:#172d35; -fx-padding:0 8 0 0; }
            .combo-box > .arrow-button { -fx-background-color:transparent; -fx-padding:0 2 0 8; }
            .combo-box .arrow { -fx-background-color:#526971; }
            .combo-box-popup .list-view { -fx-background-color:white; -fx-background-insets:0; -fx-border-color:#bdcccc; -fx-padding:3; }
            .combo-box-popup .list-cell { -fx-background-color:white; -fx-text-fill:#172d35; -fx-padding:8 10; }
            .combo-box-popup .list-cell:filled:hover, .combo-box-popup .list-cell:filled:selected { -fx-background-color:#e4f0ee; -fx-text-fill:#00575c; }
            .slider .track { -fx-background-color:#dce4e4; -fx-background-insets:0; }
            .slider .thumb { -fx-background-color:#006b70; -fx-background-insets:0; }
            .progress-indicator { -fx-progress-color:#006b70; }
        """
        layout.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
    }
    static void styleChoice(javafx.scene.control.ChoiceDialog dialog,String title,String help) {
        def heading=new javafx.scene.control.Label(title)
        heading.styleClass.add("mif-title");heading.wrapText=true
        def instruction=new javafx.scene.control.Label(help)
        instruction.styleClass.add("mif-help");instruction.wrapText=true
        def header=new javafx.scene.layout.VBox(8d,heading,instruction)
        header.padding=new javafx.geometry.Insets(16d,16d,8d,16d)
        dialog.dialogPane.header=header
        dialog.dialogPane.prefWidth=520d
        dialog.resizable=true
        stylePanel(dialog.dialogPane)
        dialog.dialogPane.lookupAll('.label').findAll {it.text==dialog.contentText}.each {
            it.styleClass.add("mif-caption");it.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        }
        dialog.dialogPane.lookupAll('.combo-box').each {
            it.minWidth=0d;it.prefWidth=360d;it.maxWidth=Double.MAX_VALUE
        }
    }

    final QuPathGUI gui
    final Map support
    final Object executor=Executors.newSingleThreadExecutor()
    final Menu menu=new Menu("mIF")
    final MenuItem review=new MenuItem("Review fitted classifier…")
    private Map drawingTools=null
    private def fittedReview=null
    private boolean closed=false
    private final javafx.beans.value.ChangeListener imageListener

    MifKronosGuiTools(QuPathGUI gui,Map support) {
        this.gui=gui;
        this.support=support
        MenuItem guide=new MenuItem("Reference annotation workflow")
        guide.setOnAction({
            information("Reference annotations",
                "1. In the pipeline's classifier step, use Annotate reference image for each included image. Save the reference regions in QuPath and close the annotation draft.\n\n"+
                "2. When the references are complete and saved, select Freeze references and fit in the pipeline.\n\n"+
                "3. Use Review fitted classifier to inspect predictions in the image regions used to develop the classifier. Return to the pipeline to record the review decision.")
        } as javafx.event.EventHandler)
        review.setOnAction({chooseModel()} as javafx.event.EventHandler)
        menu.userData=this
        menu.items.addAll(guide,review);
        gui.menuBar.menus.add(menu)
        drawingTools=support.annotationTools?.install?.call(gui,menu)
        imageListener={ observable,oldImage,newImage ->
            retirePreview();
            review.setDisable(newImage==null)
        } as javafx.beans.value.ChangeListener
        gui.imageDataProperty().addListener(imageListener)
        review.setDisable(gui.imageData==null)
    }

    private void information(String title,String text) {
        Alert alert=new Alert(Alert.AlertType.INFORMATION)
        alert.initOwner(gui.stage);
        alert.title=title;
        alert.headerText=null;
        alert.contentText=text;
        if(text.contains("\n\n")) {
            def heading=new Label(title);heading.styleClass.add("mif-title");heading.wrapText=true
            alert.dialogPane.header=new javafx.scene.layout.VBox(heading)
            alert.dialogPane.header.padding=new javafx.geometry.Insets(16d)
            def paragraphs=new javafx.scene.layout.VBox(14d)
            text.split("\n\n").each {paragraph->
                def label=new Label(paragraph);label.wrapText=true;label.maxWidth=Double.MAX_VALUE
                label.styleClass.add("mif-help");paragraphs.children.add(label)
            }
            paragraphs.padding=new javafx.geometry.Insets(8d,16d,16d,16d)
            def scroll=new ScrollPane(paragraphs)
            scroll.fitToWidth=true;scroll.hbarPolicy=ScrollPane.ScrollBarPolicy.NEVER
            alert.dialogPane.content=scroll
            alert.dialogPane.prefWidth=600d;alert.resizable=true
            stylePanel(alert.dialogPane)
        }
        alert.show()
    }

    private void chooseModel() {
        if(closed || gui.project==null || gui.imageData==null)return
        def current=gui.imageData
        def entry=gui.project.getEntry(current)
        if(entry==null) { information("Classifier review","Open an image from the current project.");
            return }
        def manager=gui.project.pixelClassifiers
        List<String> names=manager.names.toList().sort()
        if(names.isEmpty()) { information("Classifier review","There is no saved classifier yet. Complete the reference annotations and run the classifier step first.");
            return }
        ChoiceDialog<String> choice=new ChoiceDialog<>(names.first(),names)
        choice.initOwner(gui.stage);
        choice.title="Classifier review";
        choice.headerText="Select the saved classifier";
        choice.contentText="Classifier"
        styleChoice(choice,"Select a saved classifier","Inspect predictions from a fitted model in this analysis.")
        def selected=choice.showAndWait()
        if(selected.isEmpty())return
        reviewResource(selected.get())
    }

    // The shared fitted-model reviewer handles conventional and KRONOS models.
    // Replacing or closing a view releases its overlay and retained resources.
    void reviewResource(String name, Map evaluationReference=null) {
        if(closed)return
        retirePreview()
        fittedReview=support.classifierReview.open.call(gui,support+[reviewExecutor:executor,evaluationReference:evaluationReference],name)
    }

    private void retirePreview() {
        fittedReview?.close()
        fittedReview=null
    }

    @Override void close() {
        if(closed)return
        retirePreview();
        closed=true
        drawingTools?.close?.call()
        gui.imageDataProperty().removeListener(imageListener)
        gui.menuBar.menus.remove(menu);
        executor.shutdown()
    }
}
return [install:{QuPathGUI gui,Map support -> new MifKronosGuiTools(gui,support)}]
