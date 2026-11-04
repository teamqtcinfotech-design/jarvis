# ONNX Runtime's native code looks up its Java classes by name (TensorInfo, OnnxTensor, ...).
# If R8 renames or removes them the process aborts with "JNI DETECTED ERROR: java_class == null".
-keep class ai.onnxruntime.** { *; }
-keepclassmembers class ai.onnxruntime.** { *; }
-dontwarn ai.onnxruntime.**

# The service and receiver are started by name from the manifest and intents.
-keep class com.qtcinfotech.jarvis.wake.** { *; }
