package cgr.trace;

import java.util.Collection;

/** Minimal JSON string encoding; the interchange format needs nothing more. */
final class Json {

    private Json() {}

    static String string(String value) {
        StringBuilder sb = new StringBuilder(value.length() + 2);
        sb.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format(java.util.Locale.ROOT, "\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    static String strings(Collection<String> values) {
        StringBuilder sb = new StringBuilder();
        sb.append('[');
        boolean first = true;
        for (String value : values) {
            if (!first) {
                sb.append(',');
            }
            sb.append(string(value));
            first = false;
        }
        sb.append(']');
        return sb.toString();
    }
}
