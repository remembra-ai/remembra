// Task Board screen: `#/crew?project=X&view=board[&task=T-14][&lanes=agent]` (§9.1, §9.7).

import '../../components/crew/board/board.css';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { crewApi } from '../../lib/crew/api';
import { useRoute } from '../../lib/nav';
import { TaskBoard } from '../../components/crew/board/TaskBoard';
import { isSwimlane } from '../../components/crew/board/model';
import { ErrorNotice } from '../../components/relay/ui';

export function Board({ crewId, project }: { crewId: string; project: string }) {
  const crew = useCrewSocket(crewId);
  const { params } = useRoute();
  const lanes = params.get('lanes');
  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;
  return (
    <TaskBoard
      api={crewApi}
      crewId={crewId}
      project={project}
      crew={crew}
      taskParam={params.get('task')}
      lanes={isSwimlane(lanes) ? lanes : 'phase'}
    />
  );
}
