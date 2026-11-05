/*
 * CICT Fanout Manager Web UI 頁籤切換控制器（design §2、§5）。
 *
 * 極小的頁籤控制器：頁首下方的 #tabbar 有兩個按鈕（data-tab="traffic" /
 * "interconnect"），點擊即顯示對應的 .tab-panel 容器、隱藏另一個，純前端
 * 顯示／隱藏、不重載頁面。預設顯示打流頁籤（index.html 以 hidden 屬性讓
 * #tab-interconnect 預設隱藏）。
 *
 * 本檔與 app.js（打流）/ ic.js（互聯）互不干擾：各自在 DOMContentLoaded 綁
 * 自己的元素。切到互聯頁籤時派送一個 "ic-tab-shown" 事件，讓 ic.js 可在
 * 首次顯示時才載入互聯資料（lazy load），避免非互聯拓樸在背景噴錯。
 *
 * 零 build、純 vanilla JS、服出去的 /static/tabs.js。使用者面向文字為英文。
 */

"use strict";

/* 切到指定頁籤：切換按鈕的 active 樣式與容器的 hidden 屬性（design §5）。 */
function activateTab(tabName) {
    var buttons = document.querySelectorAll("#tabbar .tab-btn");
    var i;
    for (i = 0; i < buttons.length; i += 1) {
        if (buttons[i].getAttribute("data-tab") === tabName) {
            buttons[i].classList.add("active");
        } else {
            buttons[i].classList.remove("active");
        }
    }

    var trafficPanel = document.getElementById("tab-traffic");
    var icPanel = document.getElementById("tab-interconnect");
    if (tabName === "interconnect") {
        trafficPanel.hidden = true;
        icPanel.hidden = false;
        /* 首次顯示互聯頁籤時通知 ic.js 載入（lazy，避免背景噴錯）。 */
        document.dispatchEvent(new CustomEvent("ic-tab-shown"));
    } else {
        icPanel.hidden = true;
        trafficPanel.hidden = false;
    }
}

/* 綁定頁籤列按鈕的點擊事件（design §5）。 */
function wireTabs() {
    var buttons = document.querySelectorAll("#tabbar .tab-btn");
    var i;
    for (i = 0; i < buttons.length; i += 1) {
        buttons[i].addEventListener("click", (function (name) {
            return function () { activateTab(name); };
        })(buttons[i].getAttribute("data-tab")));
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireTabs);
} else {
    wireTabs();
}
