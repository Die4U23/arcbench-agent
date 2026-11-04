from pathlib import Path
import logging
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'arcbench-agent-runtime/src'))
from agent.platform_generation import main
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception('Platform source generation stopped; formal acceptance is not complete')
        print(f'PLATFORM_START_FAILED {type(exc).__name__}: {exc}', flush=True)
        raise SystemExit(1)
