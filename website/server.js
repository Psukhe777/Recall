//**
 * Express Server for Recall Website
 * Handles all routing including privacy-policy.html
 */

const express = require('express');
const path = require('path');
const app = express();

// Log all requests for debugging
app.use((req, res, next) => {
    console.log(`${new Date().toISOString()} - ${req.method} ${req.url}`);
    next();
});

// Serve static files
app.use(express.static(__dirname));

// Main routes
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'recall.html'));
});

// Privacy Policy - handle BOTH URLs
app.get('/privacy', (req, res) => {
    res.sendFile(path.join(__dirname, 'privacy-policy.html'));
});

app.get('/privacy-policy', (req, res) => {
    res.sendFile(path.join(__dirname, 'privacy-policy.html'));
});

// Also handle with .html extension
app.get('/privacy-policy.html', (req, res) => {
    res.sendFile(path.join(__dirname, 'privacy-policy.html'));
});

app.get('/compliance', (req, res) => {
    res.sendFile(path.join(__dirname, 'compliance.html'));
});

// Health check for Railway
app.get('/health', (req, res) => {
    res.json({ 
        status: 'OK', 
        timestamp: new Date().toISOString(),
        files: {
            recall: 'recall.html',
            privacy: 'privacy-policy.html',
            compliance: 'compliance.html'
        }
    });
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
                    max-width: 600px;
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
                code {
                    background: #e5e7eb;
                    padding: 0.25rem 0.5rem;
                    border-radius: 4px;
                    font-size: 0.875rem;
                }
                .debug {
                    margin-top: 2rem;
                    padding: 1rem;
                    background: #f9fafb;
                    border-radius: 8px;
                    text-align: left;
                    font-size: 0.875rem;
                }
            </style>
        </head>
        <body>
            <div class="error-container">
                <h1>404</h1>
                <p>Page not found</p>
                <p>Requested: <code>${req.url}</code></p>
                <div class="debug">
                    <strong>Available routes:</strong><br>
                    • <a href="/">/</a> (Home)<br>
                    • <a href="/privacy">/privacy</a><br>
                    • <a href="/privacy-policy">/privacy-policy</a><br>
                    • <a href="/compliance">/compliance</a><br>
                </div>
                <p style="margin-top: 2rem;"><a href="/">← Back to Recall</a></p>
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
    console.log(`   - / → recall.html`);
    console.log(`   - /privacy → privacy-policy.html`);
    console.log(`   - /privacy-policy → privacy-policy.html`);
    console.log(`   - /privacy-policy.html → privacy-policy.html`);
    console.log(`   - /compliance → compliance.html`);
    console.log(`   - /health → Health check`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('SIGTERM received, shutting down gracefully...');
    process.exit(0);
});
