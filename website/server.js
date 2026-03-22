/**
 * Simple Express Server for Railway Deployment
 * 
 * This server properly routes all your HTML pages including
 * the privacy policy, consent forms, and landing pages.
 * 
 * Deploy to Railway and all routes will work correctly.
 */

const express = require('express');
const path = require('path');
const app = express();

// Log all requests for debugging
app.use((req, res, next) => {
    console.log(`${new Date().toISOString()} - ${req.method} ${req.url}`);
    next();
});

// Serve static files (CSS, JS, images)
app.use(express.static(path.join(__dirname, 'public')));
app.use(express.static(__dirname));

// Main routes
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

app.get('/privacy', (req, res) => {
    res.sendFile(path.join(__dirname, 'privacy-policy.html'));
});

app.get('/privacy-policy', (req, res) => {
    res.sendFile(path.join(__dirname, 'privacy-policy.html'));
});

app.get('/terms', (req, res) => {
    res.sendFile(path.join(__dirname, 'terms-of-service.html'));
});

app.get('/consent', (req, res) => {
    res.sendFile(path.join(__dirname, 'patient-intake-consent.html'));
});

app.get('/admin/consent', (req, res) => {
    res.sendFile(path.join(__dirname, 'consent-admin.html'));
});

// Health check for Railway
app.get('/health', (req, res) => {
    res.json({ status: 'OK', timestamp: new Date().toISOString() });
});

// 404 handler
app.use((req, res) => {
    res.status(404).send(`
        <!DOCTYPE html>
        <html>
        <head>
            <title>Page Not Found - Recall</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    margin: 0;
                    background: #f3f4f6;
                }
                .error-container {
                    text-align: center;
                    padding: 2rem;
                }
                h1 {
                    font-size: 4rem;
                    margin: 0;
                    color: #2563eb;
                }
                p {
                    font-size: 1.25rem;
                    color: #6b7280;
                    margin: 1rem 0;
                }
                a {
                    color: #2563eb;
                    text-decoration: none;
                    font-weight: 600;
                }
                a:hover {
                    text-decoration: underline;
                }
            </style>
        </head>
        <body>
            <div class="error-container">
                <h1>404</h1>
                <p>Page not found</p>
                <p><a href="/">← Back to Recall</a></p>
            </div>
        </body>
        </html>
    `);
});

// Start server
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`✅ Recall server running on port ${PORT}`);
    console.log(`📍 Environment: ${process.env.NODE_ENV || 'development'}`);
    console.log(`🌐 Available routes:`);
    console.log(`   - /`);
    console.log(`   - /privacy`);
    console.log(`   - /terms`);
    console.log(`   - /consent`);
    console.log(`   - /admin/consent`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('SIGTERM received, shutting down gracefully...');
    process.exit(0);
});
