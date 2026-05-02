// LLM Wiki - 主脚本

// 侧边栏切换（支持桌面和移动端）
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    if (window.innerWidth <= 768) {
        // Mobile: slide in/out
        sidebar.classList.toggle('open');
        if (overlay) overlay.classList.toggle('show');
    } else {
        // Desktop: collapse/expand
        sidebar.classList.toggle('collapsed');
    }
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    console.log('LLM Wiki initialized');
    // Close sidebar on mobile when clicking outside
    const overlay = document.getElementById('sidebarOverlay');
    if (overlay) {
        overlay.addEventListener('click', () => {
            const sidebar = document.getElementById('sidebar');
            sidebar.classList.remove('open');
            overlay.classList.remove('show');
        });
    }
});