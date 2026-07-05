# utils/translations.py
TRANSLATIONS = {
    'en': {
        'start': "👋 Hello, {name}!\n\nI'm a bot for Mini War game.\nI help track market prices.",
        'choose_language': "🌍 Please choose your language:",
        'language_selected': "✅ Language set to English!",
        'language_selected_ru': "✅ Язык установлен на Русский!",
        'must_subscribe': "📢 Please subscribe to our channel to use the bot:\n{channel_link}",
        'check_subscription': "✅ Checking subscription...",
        'not_subscribed': "❌ You are not subscribed to our channel. Please subscribe and try again.",
        'subscribed': "✅ Thank you for subscribing! You can now use all features.",
        'price': "📊 Market Prices",
        'subscribe': "✅ You have subscribed to price updates!",
        'unsubscribe': "❌ You have unsubscribed from updates.",
        'target_set': "✅ Target set for {item}: ${price:,}",
        'target_reached': "🎯 TARGET REACHED!\n\n📦 {item}\n💰 Current: ${current:,}\n🎯 Target: ${target:,}",
        'best_sales': "📈 Best Sales Today\n\n",
        'admin_panel': "🔐 Admin Panel",
        'stats': "📊 Bot Statistics",
        'broadcast': "📢 Broadcast Message",
        'blacklist': "🚫 Blacklist Management",
        'user_banned': "🚫 User has been banned!",
        'user_unbanned': "✅ User has been unbanned!",
        'no_price_data': "⏳ Loading price data...",
        'price_updated': "✅ Prices updated successfully!",
        'error': "❌ An error occurred. Please try again later.",
        'not_admin': "❌ You are not an admin!",
        'best_sales_threshold': "📈 BEST SALES (+20%+ GROWTH)",
        'help': "📖 Help Information\n\nCommands:\n/start - Main menu\n/price - Current prices\n/subscribe - Subscribe\n/unsubscribe - Unsubscribe\n/targets - Your targets\n/settarget - Set target\n/removetarget - Remove target\n/admin - Admin panel\n/luchse - Best sales (admin only)"
    },
    'ru': {
        'start': "👋 Привет, {name}!\n\nЯ бот для игры Mini War.\nЯ помогаю отслеживать цены на рынке.",
        'choose_language': "🌍 Пожалуйста, выберите язык:",
        'language_selected': "✅ Язык установлен на Английский!",
        'language_selected_ru': "✅ Язык установлен на Русский!",
        'must_subscribe': "📢 Пожалуйста, подпишитесь на наш канал, чтобы использовать бота:\n{channel_link}",
        'check_subscription': "✅ Проверяю подписку...",
        'not_subscribed': "❌ Вы не подписаны на наш канал. Пожалуйста, подпишитесь и попробуйте снова.",
        'subscribed': "✅ Спасибо за подписку! Теперь вы можете использовать все функции.",
        'price': "📊 Цены на рынке",
        'subscribe': "✅ Вы подписались на обновления цен!",
        'unsubscribe': "❌ Вы отписались от обновлений.",
        'target_set': "✅ Цель установлена для {item}: ${price:,}",
        'target_reached': "🎯 ЦЕЛЬ ДОСТИГНУТА!\n\n📦 {item}\n💰 Текущая: ${current:,}\n🎯 Цель: ${target:,}",
        'best_sales': "📈 Лучшие продажи сегодня\n\n",
        'admin_panel': "🔐 Панель администратора",
        'stats': "📊 Статистика бота",
        'broadcast': "📢 Рассылка сообщения",
        'blacklist': "🚫 Управление черным списком",
        'user_banned': "🚫 Пользователь забанен!",
        'user_unbanned': "✅ Пользователь разбанен!",
        'no_price_data': "⏳ Загрузка данных о ценах...",
        'price_updated': "✅ Цены успешно обновлены!",
        'error': "❌ Произошла ошибка. Попробуйте позже.",
        'not_admin': "❌ Вы не администратор!",
        'best_sales_threshold': "📈 ЛУЧШИЕ ПРОДАЖИ (+20%+ РОСТ)",
        'help': "📖 Справка\n\nКоманды:\n/start - Главное меню\n/price - Текущие цены\n/subscribe - Подписаться\n/unsubscribe - Отписаться\n/targets - Ваши цели\n/settarget - Установить цель\n/removetarget - Удалить цель\n/admin - Панель администратора\n/luchse - Лучшие продажи (только админ)"
    }
}

def get_text(key: str, lang: str = 'en', **kwargs) -> str:
    text = TRANSLATIONS.get(lang, TRANSLATIONS['en']).get(key, TRANSLATIONS['en'].get(key, key))
    return text.format(**kwargs) if kwargs else text