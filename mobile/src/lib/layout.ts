/**
 * Which design to show. Phones get the dashboard with the floating orb; anything wide enough (tablets, or a
 * phone turned sideways) gets the two-column layout: JARVIS on the left, data on the right.
 */
import { useWindowDimensions } from 'react-native';

export const TABLET_MIN_WIDTH = 700;

export function useLayout() {
  const { width, height } = useWindowDimensions();
  const isTablet = width >= TABLET_MIN_WIDTH;
  // The assistant column: about a third, never cramped, never huge.
  const assistantWidth = Math.min(440, Math.max(320, Math.round(width * 0.34)));
  // Stat tiles per row.
  const tileColumns = width >= 1000 ? 6 : width >= TABLET_MIN_WIDTH ? 4 : 2;
  return { width, height, isTablet, assistantWidth, tileColumns, landscape: width > height };
}
