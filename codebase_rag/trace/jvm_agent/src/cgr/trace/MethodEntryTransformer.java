package cgr.trace;

import java.lang.classfile.ClassBuilder;
import java.lang.classfile.ClassElement;
import java.lang.classfile.ClassFile;
import java.lang.classfile.ClassModel;
import java.lang.classfile.ClassTransform;
import java.lang.classfile.CodeBuilder;
import java.lang.classfile.CodeElement;
import java.lang.classfile.CodeTransform;
import java.lang.classfile.MethodModel;
import java.lang.classfile.MethodTransform;
import java.lang.classfile.attribute.SourceFileAttribute;
import java.lang.classfile.instruction.LineNumber;
import java.lang.constant.ClassDesc;
import java.lang.constant.MethodTypeDesc;
import java.lang.instrument.ClassFileTransformer;
import java.lang.reflect.AccessFlag;
import java.security.ProtectionDomain;

/**
 * Injects a {@link TraceRecorder#enter} call at the entry of every method of
 * classes under the configured include prefixes. Uses the {@code
 * java.lang.classfile} API (JDK 24+), so the agent has no dependencies.
 */
final class MethodEntryTransformer implements ClassFileTransformer {

    private static final ClassDesc RECORDER = ClassDesc.of("cgr.trace.TraceRecorder");
    private static final MethodTypeDesc ENTER_DESC = MethodTypeDesc.ofDescriptor(
            "(Ljava/lang/Object;Ljava/lang/String;Ljava/lang/String;ILjava/lang/String;)V");

    private final String[] includeInternalPrefixes;

    MethodEntryTransformer(String[] includePackages) {
        includeInternalPrefixes = new String[includePackages.length];
        for (int i = 0; i < includePackages.length; i++) {
            includeInternalPrefixes[i] = includePackages[i].replace('.', '/');
        }
    }

    @Override
    public byte[] transform(
            Module module,
            ClassLoader loader,
            String internalName,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (internalName == null || !included(internalName)) {
            return null;
        }
        try {
            return instrument(internalName, classfileBuffer);
        } catch (Throwable e) {
            // A malformed or unsupported class must load uninstrumented
            // rather than break the application -- including on Errors such as
            // a LinkageError from an unparseable class file.
            System.err.println("cgr-trace-jvm: skipped " + internalName + ": " + e);
            return null;
        }
    }

    private boolean included(String internalName) {
        for (String prefix : includeInternalPrefixes) {
            // Boundary-aware: include=com/example must not match the sibling
            // package com/exampleevil, only com/example itself, members of
            // the package, or nested classes of a class named by the prefix.
            if (internalName.equals(prefix)
                    || internalName.startsWith(prefix + "/")
                    || internalName.startsWith(prefix + "$")) {
                return true;
            }
        }
        return false;
    }

    private static byte[] instrument(String internalName, byte[] bytes) {
        ClassFile cf = ClassFile.of();
        ClassModel model = cf.parse(bytes);
        String binaryName = internalName.replace('/', '.');
        String sourcePath = sourcePathOf(internalName, model);
        ClassTransform transform = (ClassBuilder builder, ClassElement element) -> {
            if (element instanceof MethodModel method && instrumentable(method)) {
                builder.transformMethod(
                        method,
                        MethodTransform.transformingCode(
                                entryInjector(binaryName, sourcePath, method)));
            } else {
                builder.with(element);
            }
        };
        return cf.transformClass(model, transform);
    }

    private static boolean instrumentable(MethodModel method) {
        return method.code().isPresent()
                && !method.flags().has(AccessFlag.ABSTRACT)
                && !method.flags().has(AccessFlag.NATIVE);
    }

    private static CodeTransform entryInjector(
            String binaryName, String sourcePath, MethodModel method) {
        String methodName = method.methodName().stringValue();
        boolean isStatic = method.flags().has(AccessFlag.STATIC);
        // Constructors must not touch `this` before the super() call, and
        // the verifier rejects an early aload_0 there anyway; record them
        // receiverless.
        boolean passReceiver = !isStatic && !"<init>".equals(methodName);
        int firstLine = method.code()
                .map(code -> code.elementStream()
                        .filter(LineNumber.class::isInstance)
                        .mapToInt(e -> ((LineNumber) e).line())
                        .min()
                        .orElse(-1))
                .orElse(-1);
        return new CodeTransform() {
            @Override
            public void atStart(CodeBuilder cb) {
                if (passReceiver) {
                    cb.aload(0);
                } else {
                    cb.aconst_null();
                }
                cb.loadConstant(binaryName);
                cb.loadConstant(methodName);
                cb.loadConstant(firstLine);
                cb.loadConstant(sourcePath);
                cb.invokestatic(RECORDER, "enter", ENTER_DESC);
            }

            @Override
            public void accept(CodeBuilder cb, CodeElement element) {
                cb.with(element);
            }
        };
    }

    private static String sourcePathOf(String internalName, ClassModel model) {
        String fileName = model.findAttribute(java.lang.classfile.Attributes.sourceFile())
                .map(SourceFileAttribute::sourceFile)
                .map(utf8 -> utf8.stringValue())
                .orElse("");
        int lastSlash = internalName.lastIndexOf('/');
        String packagePath = lastSlash < 0 ? "" : internalName.substring(0, lastSlash + 1);
        return packagePath + fileName;
    }
}
