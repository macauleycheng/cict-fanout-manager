/*
 * CICT Fanout Manager Web UI 權杖（token）模組（design §5、FEAT-003）。
 *
 * 提供一個極小的 `auth` 模組，保管「解鎖寫入動作」用的 X-API-Token。token
 * 只存在一個 JS closure 內的記憶體變數中，絕不寫入任何瀏覽器端的持久化儲存層
 * （不碰 web storage、不設 document 上的名值對），因此重新整理頁面即遺失
 * （符合「每次開頁都要重填」的需求）；也絕不被記錄或回顯。
 *
 * 同時提供一個共享的 applyWriteLock()：依 auth.hasToken() 切換
 * document.body 的 `writes-unlocked` class（空 token → 無此 class → 版面回到
 * 與今日完全一致的唯讀檢視），並呼叫 app.js / ic.js 透過
 * auth.onWriteLockChanged(cb) 註冊的回呼，讓兩個頁籤能在鎖狀態改變後各自更新
 * （例如重算按鈕 enable 狀態）。
 *
 * 零 build、純 vanilla ES5-相容 JS；必須在 app.js / ic.js 之前載入，服出去的
 * /static/token.js。使用者面向文字為英文。
 *
 * 歷史（HISTORY）：
 *   - 0.7.0  FEAT-003 初版：新增 auth 模組（記憶體內 token、getToken/setToken/
 *            clearToken/hasToken）與共享 applyWriteLock()／onWriteLockChanged()，
 *            以 body.writes-unlocked class 驅動兩頁籤的寫入按鈕可見性。
 *
 * Copyright (c) 2026 topo-manager。
 */

"use strict";

/* auth：token 保管與寫入鎖切換的單一入口（IIFE closure，不外露內部變數）。 */
var auth = (function () {
    /* token 僅存在此 closure 變數；絕不落地到任何瀏覽器儲存層。 */
    var token = "";
    /* 寫入鎖改變後要通知的回呼清單（app.js / ic.js 各註冊一個）。 */
    var listeners = [];

    /* 取回目前 token（未設時為空字串）。 */
    function getToken() {
        return token;
    }

    /* 設定 token；傳入空字串等同 clearToken()。呼叫端負責先 trim。 */
    function setToken(value) {
        token = value || "";
    }

    /* 清除 token（回到唯讀）。 */
    function clearToken() {
        token = "";
    }

    /* 是否已有可用 token（決定寫入是否解鎖）。 */
    function hasToken() {
        return token !== "";
    }

    /* 註冊一個「寫入鎖改變後」回呼（applyWriteLock 會在切換 body class 後呼叫）。 */
    function onWriteLockChanged(callback) {
        if (typeof callback === "function") {
            listeners.push(callback);
        }
    }

    /* 依目前是否有 token 通知所有已註冊回呼（供 applyWriteLock 內部使用）。 */
    function notifyWriteLockChanged() {
        for (var i = 0; i < listeners.length; i += 1) {
            listeners[i]();
        }
    }

    return {
        getToken: getToken,
        setToken: setToken,
        clearToken: clearToken,
        hasToken: hasToken,
        onWriteLockChanged: onWriteLockChanged,
        notifyWriteLockChanged: notifyWriteLockChanged
    };
})();

/* applyWriteLock：依 auth.hasToken() 切換 body 的 writes-unlocked class，然後
 * 通知所有已註冊回呼。空 token → 移除 class → 版面與今日唯讀檢視一致。 */
function applyWriteLock() {
    var unlocked = auth.hasToken();
    document.body.classList.toggle("writes-unlocked", unlocked);
    auth.notifyWriteLockChanged();
}
