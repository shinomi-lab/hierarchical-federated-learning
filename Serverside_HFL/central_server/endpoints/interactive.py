from fastapi import APIRouter, HTTPException
import asyncio
from typing import Optional
import logging

from central_server.state import set_global_model, get_global_model, clear_round_updates

router = APIRouter(prefix="/interactive", tags=["interactive"])

# インタラクティブモードの状態
interactive_mode = True
pending_next_round = False

@router.post("/confirm_next_round")
async def confirm_next_round(current_round: int):
    """次のラウンドへの進行を確認"""
    global pending_next_round
    
    if not interactive_mode:
        return {"status": "skipped", "message": "Interactive mode is disabled"}
    
    try:
        current_state = await get_global_model()
        next_round = current_round + 1
        
        print("\n" + "="*50)
        print(f"🔄 ラウンド {current_round} の処理が完了しました")
        print(f"📊 現在の状態:")
        print(f"   - 現在のラウンド: {current_round}")
        print(f"   - 次のラウンド: {next_round}")
        print("="*50)
        
        if pending_next_round:
            print("⏳ 次のラウンドへの進行待ちです...")
            return {"status": "pending", "current_round": current_round}
        
        # 非同期で確認プロンプトを表示
        async def ask_confirmation():
            global pending_next_round
            pending_next_round = True
            try:
                while True:
                    print("\n🤔 次のラウンドに進みますか？ (y/n): ", end="", flush=True)
                    response = await asyncio.get_event_loop().run_in_executor(None, input)
                    if response.lower() in ['y', 'yes']:
                        # 次のラウンドの状態を設定
                        await set_global_model(next_round, current_state["state_dict"])
                        await clear_round_updates(current_round)
                        print(f"\n✅ ラウンド {next_round} を開始します")
                        pending_next_round = False
                        return True
                    elif response.lower() in ['n', 'no']:
                        print("\n❌ ラウンドの進行をキャンセルしました")
                        pending_next_round = False
                        return False
                    print("❌ 有効な入力ではありません。'y'または'n'を入力してください。")
            except Exception as e:
                logging.error(f"確認プロセスでエラーが発生しました: {e}")
                pending_next_round = False
                return False

        # 確認プロセスを開始
        confirmation_task = asyncio.create_task(ask_confirmation())
        try:
            await asyncio.wait_for(confirmation_task, timeout=60)  # 60秒のタイムアウト
        except asyncio.TimeoutError:
            print("\n⏰ 確認がタイムアウトしました。自動的に次のラウンドに進みます。")
            await set_global_model(next_round, current_state["state_dict"])
            await clear_round_updates(current_round)
            pending_next_round = False
            return {"status": "timeout_auto_proceed", "next_round": next_round}

        return {"status": "confirmed", "next_round": next_round}
        
    except Exception as e:
        logging.error(f"ラウンド確認プロセスでエラーが発生しました: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/toggle_mode")
async def toggle_interactive_mode():
    """インタラクティブモードの切り替え"""
    global interactive_mode
    interactive_mode = not interactive_mode
    return {"interactive_mode": interactive_mode}