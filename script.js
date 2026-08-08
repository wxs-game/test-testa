const tg = window.Telegram?.WebApp;

if (tg) {
    tg.ready();
    tg.expand();
}

/* =========================
   ДАННЫЕ И БАЛАНС
========================= */

let currentBalance = 0;
let balanceMode = "deposit";
let accountStats = {
    turnover: 0,
    deposits: 0,
    withdrawals: 0
};

let selectedMethod = "CryptoBot";
let selectedMethodSub = "Криптовалюта";
let selectedMethodIcon = "cryptobot.png";

// Память ставок для каждого цвета
let colorBets = {
    green: 0,
    red: 0,
    blue: 0,
    yellow: 0,
    gold: 0
};

// Текущий выбранный цвет в табе
let activeColor = 'green';

let transactions = [];

const API_BASE_URL = (window.WXS_API_BASE_URL || "").replace(/\/+$/, "");

const COLOR_CONFIG = {
    green:  { label: '1x',  mult: 1,  color: '#2ecc71', name: 'Зеленый' },
    red:    { label: '2x',  mult: 2,  color: '#e74c3c', name: 'Красный' },
    blue:   { label: '3x',  mult: 3,  color: '#3498db', name: 'Синий' },
    yellow: { label: '5x',  mult: 5,  color: '#f1c40f', name: 'Желтый' },
    gold:   { label: '50x', mult: 50, color: '#ffd700', name: 'Золото' }
};

const sectors = [
    { type: 'gold',   ...COLOR_CONFIG.gold },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'blue',   ...COLOR_CONFIG.blue },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'yellow', ...COLOR_CONFIG.yellow },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'blue',   ...COLOR_CONFIG.blue },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'yellow', ...COLOR_CONFIG.yellow },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'blue',   ...COLOR_CONFIG.blue },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'yellow', ...COLOR_CONFIG.yellow },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'blue',   ...COLOR_CONFIG.blue },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red },
    { type: 'green',  ...COLOR_CONFIG.green },
    { type: 'red',    ...COLOR_CONFIG.red }
];

let wheelRotation = 0;
let wheelSpinning = false;

/* =========================
   TELEGRAM USER & БАЛАНС
========================= */

function loadTelegramUser() {
    if (!tg) return;
    const user = tg.initDataUnsafe?.user;
    if (!user) return;

    const usernameElement = document.getElementById("username");
    const avatarElement = document.getElementById("avatar");
    const profileName = document.getElementById("profileName");
    const profileUsername = document.getElementById("profileUsername");
    const profileAvatar = document.getElementById("profileAvatar");

    applyTelegramUser(user, {
        usernameElement,
        avatarElement,
        profileName,
        profileUsername,
        profileAvatar
    });
}

function applyTelegramUser(user, elements = {}) {
    const usernameElement = elements.usernameElement || document.getElementById("username");
    const avatarElement = elements.avatarElement || document.getElementById("avatar");
    const profileName = elements.profileName || document.getElementById("profileName");
    const profileUsername = elements.profileUsername || document.getElementById("profileUsername");
    const profileAvatar = elements.profileAvatar || document.getElementById("profileAvatar");
    const name = user.first_name || user.username || "Игрок";

    if (usernameElement) usernameElement.textContent = name;
    if (profileName) profileName.textContent = name;
    if (profileUsername) {
        profileUsername.textContent = user.username ? "@" + user.username : `ID: ${user.id}`;
    }

    if (user.photo_url) {
        const imageHTML = `<img src="${user.photo_url}" alt="avatar">`;
        if (avatarElement) avatarElement.innerHTML = imageHTML;
        if (profileAvatar) profileAvatar.innerHTML = imageHTML;
    }
}

function formatTransactionDate(timestamp) {
    if (!timestamp) return "Дата неизвестна";

    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;

    return date.toLocaleString("ru-RU", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit"
    });
}

function updateProfileStats() {
    const turnover = document.getElementById("statTurnover");
    if (turnover) turnover.textContent = `$${accountStats.turnover.toFixed(2)}`;
}

async function loadBotAccount() {
    const initData = tg?.initData;
    if (!initData) return;

    try {
        const response = await fetch(`${API_BASE_URL}/api/me`, {
            headers: {
                "X-Telegram-Init-Data": initData
            },
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error(`API request failed: ${response.status}`);
        }

        const data = await response.json();
        currentBalance = Number(data.account?.balance || 0);
        accountStats = {
            turnover: Number(data.account?.turnover || 0),
            deposits: Number(data.account?.deposits || 0),
            withdrawals: Number(data.account?.withdrawals || 0)
        };
        transactions = (data.transactions || []).map(transaction => ({
            type: transaction.type,
            method: transaction.method,
            icon: transaction.method === "xRocket" ? "xrocket.png" : "cryptobot.png",
            amount: Number(transaction.amount || 0),
            date: formatTransactionDate(transaction.timestamp),
            status: transaction.status || "success"
        }));

        if (data.user) applyTelegramUser(data.user);
        updateBalance();
        updateProfileStats();
        renderTransactions();
    } catch (error) {
        console.error("Не удалось загрузить данные бота:", error);
        showMessage("Не удалось загрузить баланс. Откройте Mini App из Telegram ещё раз.");
    }
}

function updateBalance() {
    const value = currentBalance.toFixed(2) + " $";

    const topBalance = document.getElementById("topBalance");
    const balanceCardValue = document.getElementById("balanceCardValue");
    const profileBalance = document.getElementById("profileBalance");
    const betBalanceText = document.getElementById("betBalanceText");

    if (topBalance) topBalance.textContent = value;
    if (balanceCardValue) balanceCardValue.textContent = value;
    if (profileBalance) profileBalance.textContent = value;
    if (betBalanceText) betBalanceText.textContent = `Баланс: ${value}`;
    
    updateTotalBet();
}

/* =========================
   НАВИГАЦИЯ
========================= */

function hideAllPages() {
    const pages = ["homePage", "wheelPage", "balancePage", "profilePage", "bonusPage"];
    pages.forEach(id => {
        const page = document.getElementById(id);
        if (page) page.classList.add("hidden");
    });
    closeMethodsDropdown();
}

function showPage(id) {
    hideAllPages();
    const page = document.getElementById(id);
    if (page) page.classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
}

function goHome() {
    showPage("homePage");
    updateNav("home");
    loadTelegramUser();
}

function openGamesMenu() {
    const homePage = document.getElementById('homePage');
    const gamesSection = document.getElementById('gamesListSection');

    if (homePage && homePage.classList.contains('hidden')) {
        showPage("homePage");
    }

    updateNav("games");

    if (gamesSection) {
        gamesSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function openWheel() {
    showPage("wheelPage");
    updateNav("games");
    updateBalance();
    loadTelegramUser(); // Обновление данных юзера при входе в игру
    drawWheel();
    renderColorTabs();
    selectColorTab(activeColor);
}

function openBalance(mode = "deposit") {
    showPage("balancePage");
    setBalanceMode(mode);
    updateNav("balance");
    updateBalance();
    renderTransactions();
}

function openProfile() {
    showPage("profilePage");
    updateNav("profile");
    updateBalance();
    loadTelegramUser();
}

function openBonus() {
    showPage("bonusPage");
    updateNav("bonus");
}

function updateNav(active) {
    const navItems = document.querySelectorAll(".nav-item");
    navItems.forEach(item => item.classList.remove("active"));

    const map = { home: "homeNav", games: "gamesNav", balance: "balanceNav", bonus: "bonusNav", profile: "profileNav" };
    const activeElement = document.getElementById(map[active]);
    if (activeElement) activeElement.classList.add("active");
}

/* =========================
   ОТРИСОВКА SVG КОЛЕСА
========================= */

function drawWheel() {
    const wheelSvg = document.getElementById('wheelSvg');
    const rewardList = document.getElementById('rewardList');
    if (!wheelSvg) return;

    const total = sectors.length;
    const sliceAngle = 360 / total;
    const radius = 150;
    const center = 150;

    let svgContent = '';

    sectors.forEach((sector, i) => {
        const startAngle = i * sliceAngle - 90;
        const endAngle = startAngle + sliceAngle;

        const x1 = center + radius * Math.cos((Math.PI * startAngle) / 180);
        const y1 = center + radius * Math.sin((Math.PI * startAngle) / 180);
        const x2 = center + radius * Math.cos((Math.PI * endAngle) / 180);
        const y2 = center + radius * Math.sin((Math.PI * endAngle) / 180);

        const pathData = `M ${center} ${center} L ${x1} ${y1} A ${radius} ${radius} 0 0 1 ${x2} ${y2} Z`;

        const textAngle = startAngle + sliceAngle / 2;
        const textRadius = radius * 0.75;
        const textX = center + textRadius * Math.cos((Math.PI * textAngle) / 180);
        const textY = center + textRadius * Math.sin((Math.PI * textAngle) / 180);

        svgContent += `
            <path d="${pathData}" fill="${sector.color}" class="wheel-sector" />
            <text x="${textX}" y="${textY}" class="wheel-sector-text" style="font-size: 8px;" transform="rotate(${textAngle + 90}, ${textX}, ${textY})">
                ${sector.label}
            </text>
        `;
    });

    wheelSvg.innerHTML = svgContent;

    if (rewardList) {
        rewardList.innerHTML = Object.keys(COLOR_CONFIG).map(key => {
            const cfg = COLOR_CONFIG[key];
            return `
                <div style="background:#161616; border:1px solid #222; padding:10px; border-radius:12px; display:flex; align-items:center; gap:8px;">
                    <span style="background:${cfg.color}; width:16px; height:16px; border-radius:50%; display:inline-block;"></span>
                    <b style="font-size:12px;">${cfg.name} (${cfg.label})</b>
                </div>
            `;
        }).join('');
    }
}

/* =========================
   ТАБЫ ЦВЕТОВ И СТАВКИ
========================= */

function renderColorTabs() {
    const row = document.getElementById('colorTabsRow');
    if (!row) return;

    row.innerHTML = Object.keys(COLOR_CONFIG).map(key => {
        const cfg = COLOR_CONFIG[key];
        const hasBet = colorBets[key] > 0;
        
        return `
            <div class="color-tab-btn ${key === activeColor ? 'active' : ''}" 
                 id="tab-${key}" 
                 onclick="selectColorTab('${key}')">
                <span class="tab-indicator" style="background: ${cfg.color};"></span>
                <span class="tab-name">${cfg.name}</span>
                <span class="tab-mult" id="tab-val-${key}">
                    ${hasBet ? colorBets[key] + ' $' : cfg.label}
                </span>
            </div>
        `;
    }).join('');
}

function selectColorTab(color) {
    if (wheelSpinning) return;

    const currentInput = document.getElementById('activeBetInput');
    if (currentInput) {
        onActiveColorInput(currentInput.value);
    }

    activeColor = color;

    document.querySelectorAll('.color-tab-btn').forEach(btn => btn.classList.remove('active'));
    const currentTab = document.getElementById(`tab-${color}`);
    if (currentTab) currentTab.classList.add('active');

    const cfg = COLOR_CONFIG[color];
    const titleBox = document.getElementById('activeColorTitle');
    if (titleBox) {
        titleBox.innerHTML = `
            <span class="color-indicator" style="background: ${cfg.color};"></span>
            <div>
                <span>Ставка на ${cfg.name}</span><br>
                <span style="color: #888888; font-size: 11px; font-weight: 700;">(${cfg.label})</span>
            </div>
        `;
    }

    if (currentInput) {
        const savedVal = colorBets[color];
        currentInput.value = savedVal > 0 ? savedVal : '';
    }

    updateTotalBet();
}

function onActiveColorInput(val) {
    let parsed = parseFloat(val);
    if (isNaN(parsed) || parsed <= 0) {
        colorBets[activeColor] = 0;
    } else {
        colorBets[activeColor] = parsed;
    }

    const tabVal = document.getElementById(`tab-val-${activeColor}`);
    if (tabVal) {
        const cfg = COLOR_CONFIG[activeColor];
        tabVal.textContent = colorBets[activeColor] > 0 ? `${colorBets[activeColor]} $` : cfg.label;
    }

    updateTotalBet();
}

// Минимальная ставка 0.10 $
function applyMinToActive() {
    if (wheelSpinning) return;
    colorBets[activeColor] = 0.10;

    const input = document.getElementById('activeBetInput');
    if (input) input.value = '0.1';

    const tabVal = document.getElementById(`tab-val-${activeColor}`);
    if (tabVal) tabVal.textContent = '0.1 $';

    updateTotalBet();
}

// Процентная ставка (от 10% до MAX)
function applyPercentToActive(pct) {
    if (wheelSpinning) return;

    let otherBetsSum = 0;
    Object.keys(colorBets).forEach(key => {
        if (key !== activeColor) otherBetsSum += colorBets[key];
    });

    const availableBalance = currentBalance - otherBetsSum;
    if (availableBalance <= 0) return;

    const amount = Math.floor(availableBalance * pct * 100) / 100;
    colorBets[activeColor] = amount;

    const input = document.getElementById('activeBetInput');
    if (input) input.value = amount > 0 ? amount : '';

    const tabVal = document.getElementById(`tab-val-${activeColor}`);
    if (tabVal) {
        const cfg = COLOR_CONFIG[activeColor];
        tabVal.textContent = amount > 0 ? `${amount} $` : cfg.label;
    }

    updateTotalBet();
}

// Сброс ставки для активного цвета
function resetActiveBet() {
    if (wheelSpinning) return;
    colorBets[activeColor] = 0;

    const input = document.getElementById('activeBetInput');
    if (input) input.value = '';

    const tabVal = document.getElementById(`tab-val-${activeColor}`);
    if (tabVal) {
        const cfg = COLOR_CONFIG[activeColor];
        tabVal.textContent = cfg.label;
    }

    updateTotalBet();
}

function updateTotalBet() {
    const totalInfo = document.getElementById('totalBetInfo');
    let totalSum = 0;
    
    Object.values(colorBets).forEach(val => {
        totalSum += val;
    });

    if (totalInfo) {
        totalInfo.textContent = `Общая ставка: ${totalSum.toFixed(2)} $`;
    }
}

/* =========================
   ВРАЩЕНИЕ С ПЛАВНЫМ ЗУМОМ
========================= */

async function spinWheel() {
    showMessage("Ставки доступны в Telegram-боте. Сайт показывает аккаунт и историю операций.");
    return;
    /*
    if (wheelSpinning) return;

    let totalBet = 0;
    Object.values(colorBets).forEach(v => totalBet += v);

    if (totalBet <= 0) {
        showMessage("Укажите сумму хотя бы на один цвет!");
        return;
    }

    if (totalBet > currentBalance) {
        showMessage("Недостаточно средств на балансе!");
        return;
    }

    const button = document.getElementById('spinButton');
    const result = document.getElementById('wheelResult');
    const resultValue = document.getElementById('resultValue');
    const wheelSvg = document.getElementById('wheelSvg');
    const wheelStage = document.querySelector('.wheel-stage');

    if (!button || !wheelSvg) return;

    button.disabled = true;
    currentBalance -= totalBet;
    updateBalance();

    wheelSpinning = true;
    button.innerHTML = '<span>↻ Вращение...</span>';

    if (result) result.classList.remove('show');
    if (resultValue) resultValue.textContent = '?';

    const rewardIndex = Math.floor(Math.random() * sectors.length);
    const totalSectors = sectors.length;
    const sectorAngle = 360 / totalSectors;

    const padding = 0.15; 
    const randomOffset = (Math.random() * (1 - 2 * padding) + padding) * sectorAngle;

    const targetAngleInSector = (rewardIndex * sectorAngle) + randomOffset;
    const stopAngle = 360 - targetAngleInSector;

    const fullSpins = 6;
    wheelRotation += fullSpins * 360 + (stopAngle - (wheelRotation % 360));

    wheelSvg.style.transform = `rotate(${wheelRotation}deg)`;

    setTimeout(() => {
        if (wheelStage) wheelStage.classList.add('zoomed');
    }, 3600);

    setTimeout(() => {
        wheelSpinning = false;
        button.disabled = false;
        button.innerHTML = '<span>↻ Сделать ставку</span>';

        if (wheelStage) wheelStage.classList.remove('zoomed');

        const wonSector = sectors[rewardIndex];
        const betOnWonColor = colorBets[wonSector.type] || 0;

        let totalWin = 0;
        if (betOnWonColor > 0) {
            totalWin = betOnWonColor * wonSector.mult;
            currentBalance += totalWin;
            updateBalance();
        }

        if (resultValue) {
            if (totalWin > 0) {
                resultValue.textContent = `Победа +${totalWin.toFixed(2)} $ (${wonSector.label})`;
                resultValue.style.color = '#2ecc71';
            } else {
                resultValue.textContent = `Выпал ${wonSector.name} (${wonSector.label})`;
                resultValue.style.color = '#e74c3c';
            }
        }

        if (result) result.classList.add('show');

        if (tg?.HapticFeedback) {
            tg.HapticFeedback.notificationOccurred(totalWin > 0 ? "success" : "error");
        }

    }, 5000);
    */
}

/* =========================
   ПОПОЛНЕНИЕ И ВЫВОД
========================= */

function renderTransactions() {
    const list = document.getElementById("historyList");
    const count = document.getElementById("txCount");
    if (!list) return;

    if (count) count.textContent = `${transactions.length} операций`;

    if (transactions.length === 0) {
        list.innerHTML = `<div style="text-align:center; color:#666; font-size:13px; padding:15px;">История пуста</div>`;
        return;
    }

    list.innerHTML = transactions.map(tx => {
        const isDep = tx.type === 'deposit';
        const sign = isDep ? '+' : '-';
        const title = isDep ? 'Пополнение' : 'Вывод средств';
        const statusText = tx.status === 'success' ? 'Успешно' : 'В обработке';

        // Проверяем, является ли icon файлом картинки
        const iconHTML = tx.icon.includes('.')
            ? `<img src="${tx.icon}" style="width: 16px; height: 16px; object-fit: contain; vertical-align: middle;">`
            : tx.icon;

        return `
            <div class="history-item">
                <div class="tx-left">
                    <div class="tx-icon ${tx.type}">
                        ${isDep ? '↙' : '↗'}
                    </div>
                    <div class="tx-details">
                        <span class="tx-title">${title}</span>
                        <span class="tx-subtitle">${iconHTML} ${tx.method} • ${tx.date}</span>
                    </div>
                </div>
                <div class="tx-right">
                    <span class="tx-amount ${tx.type}">${sign}${tx.amount.toFixed(2)} $</span>
                    <span class="tx-status ${tx.status}">${statusText}</span>
                </div>
            </div>
        `;
    }).join('');
}

function closeMethodsDropdown() {
    const dropdown = document.getElementById("methodsDropdown");
    const arrow = document.getElementById("methodArrow");
    if (dropdown) dropdown.classList.remove("open");
    if (arrow) arrow.textContent = "▼";
}

function setBalanceMode(mode) {
    balanceMode = mode;
    closeMethodsDropdown();

    const depositTab = document.getElementById("depositTab");
    const withdrawTab = document.getElementById("withdrawTab");
    const title = document.getElementById("formTitle");
    const subtitle = document.getElementById("formSubtitle");
    const action = document.getElementById("balanceAction");

    if (!depositTab || !withdrawTab || !title || !subtitle || !action) return;

    depositTab.classList.remove("active");
    withdrawTab.classList.remove("active");

    if (mode === "deposit") {
        depositTab.classList.add("active");
        title.textContent = "Пополнение баланса";
        subtitle.textContent = "Выберите удобный способ пополнения";
        action.textContent = "Пополнить";
    }

    if (mode === "withdraw") {
        withdrawTab.classList.add("active");
        title.textContent = "Вывод средств";
        subtitle.textContent = "Выберите способ вывода средств";
        action.textContent = "Вывести";
    }
}

function toggleMethods() {
    const dropdown = document.getElementById("methodsDropdown");
    const arrow = document.getElementById("methodArrow");

    if (!dropdown) return;
    dropdown.classList.toggle("open");

    if (arrow) {
        arrow.textContent = dropdown.classList.contains("open") ? "▲" : "▼";
    }
}

function selectMethod(method, icon, sub) {
    selectedMethod = method;
    selectedMethodIcon = icon;
    selectedMethodSub = sub;

    const selected = document.getElementById("selectedMethod");
    const selectedSub = document.getElementById("selectedMethodSub");
    const iconElement = document.getElementById("selectedMethodIcon");

    if (selected) selected.textContent = method;
    if (selectedSub) selectedSub.textContent = sub;

    // Обновление аватарки/иконки метода (картинка или текст)
    if (iconElement) {
        if (icon.includes('.')) {
            iconElement.src = icon;
        } else {
            iconElement.textContent = icon;
        }
    }

    closeMethodsDropdown();
}

function demoBalanceAction() {
    showMessage("Пополнение и вывод выполняются через бота. Здесь отображаются данные из общей базы.");
}

function claimBonus() {
    showMessage("Демо: ежедневный бонус пока не подключён");
}

function showMessage(text) {
    if (tg?.showAlert) {
        tg.showAlert(text);
        return;
    }
    alert(text);
}

/* =========================
   ЗАПУСК
========================= */

document.addEventListener("DOMContentLoaded", () => {
    loadTelegramUser();
    updateBalance();
    renderTransactions();
    loadBotAccount();
    goHome();
});

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        loadBotAccount();
    }
});
