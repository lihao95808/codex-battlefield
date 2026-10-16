import { LeftRail } from './LeftRail';
import { WorkspaceView } from './WorkspaceView';
import { useBattlefieldController } from './useBattlefieldController';

export function App() {
  const view = useBattlefieldController();
  return (
    <main className="shell" data-testid="app-shell" data-busy={view.busy ? '1' : '0'}>
      <LeftRail view={view} />

      <WorkspaceView view={view} />
    </main>
  );
}
