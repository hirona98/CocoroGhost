"""cocoro_ghost 起動スクリプト"""
import sys
from pathlib import Path

# プロジェクトルートをPYTHONPATHに追加
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("cocoro_ghost.main:app", host="0.0.0.0", port=55601, reload=False)
