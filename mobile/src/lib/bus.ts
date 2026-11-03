// Tiny event bus so the dashboard and the alert engine refresh together after any change.
type Listener = () => void;
const listeners = new Set<Listener>();

export const dataChanged = {
  emit() {
    listeners.forEach((l) => l());
  },
  subscribe(l: Listener) {
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  },
};
