// No images, models, cell libraries, or network acquisition are used.
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.StreamReadFeature
if(args.length!=2)throw new IllegalArgumentException('Expected request and result')
Path source=Path.of(args[0]),result=Path.of(args[1])
if(Files.size(source)>65536||Files.exists(result))throw new IllegalArgumentException('Invalid check files')
def mapper=new ObjectMapper(JsonFactory.builder().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
Map request=mapper.readValue(Files.readString(source),Map.class)
if(request.keySet()!=['schema','challenge','extension','sha256','version'] as Set || request.schema!='warpy-runtime-check/v1' ||
    !(request.challenge==~/[0-9a-f]{64}/)||request.version!='0.5.0')throw new IllegalArgumentException('Invalid check request')
def loader=qupath.lib.gui.QuPathGUI.getExtensionCatalogManager().extensionClassLoader
Class type=loader.loadClass('qupath.ext.warpy.WarpyExtension')
Path jar=Path.of(type.protectionDomain.codeSource.location.toURI())
if(jar!=Path.of(request.extension)||type.package.implementationVersion!=request.version||Files.isSymbolicLink(jar))
    throw new IllegalArgumentException('Warpy loaded from another installation')
String digest=MessageDigest.getInstance('SHA-256').digest(Files.readAllBytes(jar)).encodeHex().toString()
if(digest!=request.sha256)throw new IllegalArgumentException('Warpy differs from the verified release')
def shell=new GroovyShell(loader,new Binding([directory:result.parent]))
shell.evaluate("""
import net.imglib2.realtransform.AffineTransform3D
import net.imglib2.realtransform.RealTransform
import qupath.ext.imagecombinerwarpy.realtransform.RealTransformSerializer
import qupath.ext.warpy.Warpy
import java.nio.file.Files
AffineTransform3D transform=new AffineTransform3D();transform.translate(7d,11d,0d)
def path=directory.resolve('transform.json')
Files.writeString(path,RealTransformSerializer.getRealTransformAdapter().toJson(transform,RealTransform.class))
def saved=Warpy.getRealTransform(path.toFile());double[] result=new double[3]
saved.apply([3d,5d,0d] as double[],result)
if(Math.abs(result[0]-10d)>1e-10||Math.abs(result[1]-16d)>1e-10)throw new IllegalArgumentException('Transformation check failed')
""")
Files.writeString(result,mapper.writeValueAsString(request+[qupath_version:qupath.lib.common.GeneralTools.getVersion(),transform_checked:true]),StandardOpenOption.CREATE_NEW)
