package com.pshea.permissivecodex

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.core.content.ContextCompat
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.pshea.permissivecodex.ui.ArtifactLink
import com.pshea.permissivecodex.ui.CompanionApp
import com.pshea.permissivecodex.ui.PermissiveTheme

class MainActivity : ComponentActivity() {
    private val viewModel: CompanionViewModel by viewModels()
    private var cameraAction: (() -> Unit)? = null
    private var pendingArtifact: ArtifactLink? = null
    private val scanner = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let(viewModel::pair)
    }
    private val cameraPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val action = cameraAction; cameraAction = null
        if (granted) action?.invoke() else viewModel.reportError("Camera access is needed for local QR scanning or taking a photo. You can paste the invitation or attach a file instead.")
    }
    private val documents = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris -> if (uris.isNotEmpty()) viewModel.addAttachments(uris) }
    private val photo = registerForActivityResult(ActivityResultContracts.TakePicture()) { success -> viewModel.completePhoto(success) }
    private val artifactDestination = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
        val artifact = pendingArtifact; pendingArtifact = null
        if (uri != null && artifact != null) viewModel.saveArtifact(artifact, uri)
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            PermissiveTheme {
                CompanionApp(viewModel,
                    onScan = { withCameraPermission { scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE).setPrompt("Scan the pairing invitation on Windows").setBeepEnabled(false).setOrientationLocked(false)) } },
                    onAddFile = { documents.launch(arrayOf("*/*")) },
                    onTakePhoto = { withCameraPermission { runCatching { photo.launch(viewModel.preparePhoto()) }.onFailure { viewModel.completePhoto(false); viewModel.reportError("Could not open the camera app") } } },
                    onSaveArtifact = { artifact -> pendingArtifact = artifact; artifactDestination.launch(artifact.name) },
                )
            }
        }
    }
    private fun withCameraPermission(action: () -> Unit) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) action()
        else { cameraAction = action; cameraPermission.launch(Manifest.permission.CAMERA) }
    }
    override fun onStart() { super.onStart(); viewModel.onForeground() }
    override fun onStop() { viewModel.onBackground(); super.onStop() }
}
