import Ionicons from '@expo/vector-icons/Ionicons';
import React from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleProp,
  StyleSheet,
  Text,
  TextProps,
  TextStyle,
  View,
  ViewStyle,
} from 'react-native';

import { colors, fontFor, fonts, priorityColor, priorityLabel, prioritySoft, radius, shadow } from '@/lib/theme';

type Variant = 'display' | 'title' | 'heading' | 'body' | 'small' | 'label' | 'caption';

const variantStyle: Record<Variant, TextStyle> = {
  display: { fontSize: 27, fontWeight: '700', color: colors.ink, letterSpacing: -0.4, lineHeight: 36 },
  title: { fontSize: 20, fontWeight: '600', color: colors.ink, letterSpacing: -0.2, lineHeight: 28 },
  heading: { fontSize: 15, fontWeight: '600', color: colors.text, lineHeight: 22 },
  body: { fontSize: 15, color: colors.text, lineHeight: 23 },
  small: { fontSize: 13, color: colors.muted, lineHeight: 19 },
  label: { fontSize: 11, fontWeight: '600', color: colors.muted, letterSpacing: 0.8, textTransform: 'uppercase' },
  caption: { fontSize: 12, color: colors.faint, lineHeight: 16 },
};

/** Text in Poppins. The weight asked for (in the variant or the style) picks the matching font file. */
export function Txt({ variant = 'body', style, ...rest }: TextProps & { variant?: Variant }) {
  const flat = StyleSheet.flatten([variantStyle[variant], style]) as TextStyle;
  const family = flat.fontFamily && flat.fontFamily.startsWith('Poppins') ? flat.fontFamily : fontFor(flat.fontWeight);
  return <Text {...rest} style={[flat, { fontFamily: family }]} />;
}

export function Icon({ name, size = 20, color = colors.ink }: { name: string; size?: number; color?: string }) {
  return <Ionicons name={name as any} size={size} color={color} />;
}

export function Card({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  return <View style={[styles.card, style]}>{children}</View>;
}

type ButtonKind = 'primary' | 'secondary' | 'ghost' | 'danger' | 'gold';

export function Button({
  title,
  onPress,
  kind = 'primary',
  icon,
  disabled,
  loading,
  style,
  compact,
}: {
  title: string;
  onPress: () => void;
  kind?: ButtonKind;
  icon?: string;
  disabled?: boolean;
  loading?: boolean;
  style?: StyleProp<ViewStyle>;
  compact?: boolean;
}) {
  const palette = {
    primary: { bg: colors.ink, fg: colors.white, border: colors.ink },
    secondary: { bg: colors.surface, fg: colors.ink, border: colors.line },
    ghost: { bg: 'transparent', fg: colors.inkSoft, border: 'transparent' },
    danger: { bg: colors.surface, fg: colors.danger, border: colors.dangerSoft },
    gold: { bg: colors.gold, fg: colors.white, border: colors.gold },
  }[kind];
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      disabled={disabled || loading}
      style={({ pressed }) => [
        styles.button,
        compact && styles.buttonCompact,
        { backgroundColor: palette.bg, borderColor: palette.border, opacity: disabled ? 0.45 : pressed ? 0.85 : 1 },
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={palette.fg} />
      ) : (
        <View style={[styles.row, { maxWidth: '100%' }]}>
          {icon ? <Icon name={icon} size={compact ? 16 : 18} color={palette.fg} /> : null}
          {/* one line always: a label never wraps or pushes its neighbours off a narrow card */}
          <Text numberOfLines={1} style={[styles.buttonText, compact && { fontSize: 14 }, { color: palette.fg, marginLeft: icon ? 8 : 0, flexShrink: 1 }]}>
            {title}
          </Text>
        </View>
      )}
    </Pressable>
  );
}

export function Chip({
  label,
  selected,
  onPress,
  color = colors.ink,
  icon,
}: {
  label: string;
  selected?: boolean;
  onPress?: () => void;
  color?: string;
  icon?: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={!onPress}
      style={[
        styles.chip,
        selected ? { backgroundColor: color, borderColor: color } : { backgroundColor: colors.surface, borderColor: colors.line },
      ]}
    >
      {icon ? <Icon name={icon} size={14} color={selected ? colors.white : color} /> : null}
      <Text style={[styles.chipText, { color: selected ? colors.white : colors.text, marginLeft: icon ? 6 : 0 }]}>{label}</Text>
    </Pressable>
  );
}

export function PriorityTag({ priority }: { priority: string }) {
  return (
    <View style={[styles.tag, { backgroundColor: prioritySoft[priority] }]}>
      <Text style={[styles.tagText, { color: priorityColor[priority] }]}>{priorityLabel[priority] ?? priority}</Text>
    </View>
  );
}

export function Tag({ label, color = colors.muted, bg = colors.surfaceAlt, icon }: { label: string; color?: string; bg?: string; icon?: string }) {
  return (
    <View style={[styles.tag, { backgroundColor: bg }]}>
      {icon ? <Icon name={icon} size={11} color={color} /> : null}
      <Text style={[styles.tagText, { color, marginLeft: icon ? 4 : 0 }]}>{label}</Text>
    </View>
  );
}

export function SectionHeader({ title, count, action }: { title: string; count?: number; action?: React.ReactNode }) {
  return (
    <View style={styles.sectionHeader}>
      <View style={styles.row}>
        <Txt variant="label">{title}</Txt>
        {count !== undefined ? <Txt variant="caption" style={{ marginLeft: 8 }}>{count}</Txt> : null}
      </View>
      {action}
    </View>
  );
}

export function IconButton({ icon, onPress, label, color = colors.ink }: { icon: string; onPress: () => void; label: string; color?: string }) {
  return (
    <Pressable accessibilityLabel={label} onPress={onPress} hitSlop={10} style={({ pressed }) => [styles.iconButton, pressed && { opacity: 0.6 }]}>
      <Icon name={icon} size={22} color={color} />
    </Pressable>
  );
}

export function Empty({ text }: { text: string }) {
  return (
    <View style={styles.empty}>
      <Txt variant="small" style={{ textAlign: 'center' }}>{text}</Txt>
    </View>
  );
}

export const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center' },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
    padding: 16,
    ...shadow,
  },
  button: {
    minHeight: 50,
    borderRadius: radius.md,
    borderWidth: 1,
    paddingHorizontal: 18,
    alignItems: 'center',
    justifyContent: 'center',
  },
  buttonCompact: { minHeight: 38, paddingHorizontal: 12, borderRadius: radius.sm },
  buttonText: { fontSize: 14, fontFamily: fonts.semibold, letterSpacing: 0.2 },
  chip: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 12,
    paddingVertical: 7,
    borderRadius: 999,
    borderWidth: 1,
    marginRight: 8,
    marginBottom: 8,
  },
  chipText: { fontSize: 13, fontFamily: fonts.medium },
  tag: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 8, paddingVertical: 3, borderRadius: 6, marginRight: 6 },
  tagText: { fontSize: 11, fontFamily: fonts.semibold, letterSpacing: 0.3 },
  sectionHeader: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 22, marginBottom: 10 },
  iconButton: { width: 44, height: 44, alignItems: 'center', justifyContent: 'center', borderRadius: 22 },
  empty: { paddingVertical: 18, paddingHorizontal: 12, borderRadius: radius.md, borderWidth: 1, borderStyle: 'dashed', borderColor: colors.line },
});
